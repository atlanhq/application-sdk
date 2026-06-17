"""
SQL client implementation for database connections.

This module provides SQL client classes for both synchronous and asynchronous
database operations, supporting batch processing and server-side cursors.
"""

import asyncio
import concurrent
import hashlib
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, Union, cast
from urllib.parse import quote_plus

from application_sdk.clients._interface import ClientInterface
from application_sdk.clients.models import DatabaseConfig
from application_sdk.clients.sql_errors import (
    EngineNotInitializedError,
    InvalidSqlEngineTypeError,
    MissingSqlParamError,
    SqlAwsCredentialsError,
    SqlClientAuthFailedError,
    SqlClientConfigError,
    SqlCredentialsParseError,
    SqlPandasResultError,
    UnsupportedSqlCursorError,
)
from application_sdk.clients.sql_typecasters import install_tolerant_text_decoder_hook
from application_sdk.common.aws_utils import (
    generate_aws_rds_token_with_iam_role,
    generate_aws_rds_token_with_iam_user,
)
from application_sdk.constants import AWS_SESSION_NAME, USE_SERVER_SIDE_CURSOR
from application_sdk.credentials.utils import parse_credentials_extra
from application_sdk.errors import AppError, sanitize_cause_repr
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

if TYPE_CHECKING:
    import daft
    import pandas as pd
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
    from sqlalchemy.orm import Session


class BaseSQLClient(ClientInterface):
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
    credentials: dict[str, Any]
    resolved_credentials: dict[str, Any]
    use_server_side_cursor: bool = USE_SERVER_SIDE_CURSOR
    DB_CONFIG: DatabaseConfig | None = None

    def __init__(
        self,
        use_server_side_cursor: bool = USE_SERVER_SIDE_CURSOR,
        credentials: dict[str, Any] | None = None,
        chunk_size: int = 5000,
    ):
        """
        Initialize the SQL client.

        Args:
            use_server_side_cursor (bool, optional): Whether to use server-side cursors.
                Defaults to USE_SERVER_SIDE_CURSOR.
            credentials (Optional[Dict[str, Any]], optional): Database credentials.
                Defaults to None, which is treated as an empty dict.
        """
        self.use_server_side_cursor = use_server_side_cursor
        self.credentials = credentials if credentials is not None else {}
        self.resolved_credentials = {}
        self.chunk_size = chunk_size

    async def load(self, credentials: dict[str, Any]) -> None:
        """Load credentials and prepare engine for lazy connections.

        This method now only stores credentials and creates the engine without
        establishing a persistent connection. Connections are created on-demand.

        Args:
            credentials (Dict[str, Any]): Database connection credentials.

        Raises:
            SqlClientAuthFailedError: If credentials are invalid or engine creation fails.
        """
        if not self.DB_CONFIG:
            raise SqlClientConfigError()

        self.credentials = credentials  # Update the instance credentials
        try:
            from sqlalchemy import (  # noqa: PLC0415 — optional dep: sqlalchemy
                create_engine,
            )

            # Create engine but no persistent connection
            self.engine = create_engine(
                self.get_sqlalchemy_connection_string(),
                connect_args=self.DB_CONFIG.connect_args,
                pool_pre_ping=self.DB_CONFIG.pool_pre_ping,
            )

            # Install a tolerant UTF-8 text decoder on every new psycopg2/psycopg3
            # connection so query-history rows containing non-UTF-8 bytes (e.g.
            # 0x96 from Windows-1252 paste) yield U+FFFD instead of raising
            # UnicodeDecodeError. Tracking: WARE-970.
            install_tolerant_text_decoder_hook(self.engine)

            # Test connection briefly to validate credentials.
            # Wrapped in asyncio.to_thread because SQLAlchemy's synchronous
            # engine.connect() blocks the event loop — critical for Temporal
            # activities where blocking starves the auto-heartbeat.
            # Capture engine in a local variable so the closure doesn't need to
            # re-read self.engine (which is typed Optional) and pyright can narrow it.
            _engine = self.engine

            def _ping() -> None:
                with _engine.connect() as _:
                    pass  # Connection test successful

            await asyncio.to_thread(_ping)

            # Don't store persistent connection
            self.connection = None

        # conformance: ignore[E004] exc_info omitted intentionally; SQLAlchemy embeds password in traceback; cause is sanitized and re-raised as typed error
        except Exception as e:
            # No exc_info here: SQLAlchemy errors embed the full connection
            # string (including the password) in their message, and the
            # traceback would print it verbatim into logs.
            logger.error(  # conformance: ignore[E005] exc_info would expose SQLAlchemy password in traceback; cause sanitized above
                "Error loading SQL client: %s", sanitize_cause_repr(e)
            )
            if self.engine:
                self.engine.dispose()
                self.engine = None
            raise SqlClientAuthFailedError(cause=e) from e

    async def close(self) -> None:
        """Close the database connection."""
        if self.engine:
            self.engine.dispose()
            self.engine = None
        self.connection = None  # Should already be None, but ensure cleanup

    def get_iam_user_token(self):
        """Get an IAM user token for AWS RDS database authentication.

        This method generates a temporary authentication token for IAM user-based
        authentication with AWS RDS databases. It requires AWS access credentials
        and database connection details.

        Returns:
            str: A temporary authentication token for database access.

        Raises:
            SqlCredentialsParseError: If required credentials (username or database) are missing.
        """
        extra = parse_credentials_extra(self.credentials)
        aws_access_key_id = self.credentials.get("username")
        aws_secret_access_key = self.credentials.get("password")
        host = self.credentials.get("host")
        user = extra.get("username")
        database = extra.get("database")
        if not user:
            raise SqlCredentialsParseError(
                field="username",
                message="username is required for IAM user authentication",
            )
        if not database:
            raise SqlCredentialsParseError(
                field="database",
                message="database is required for IAM user authentication",
            )

        port = self.credentials.get("port")
        region = self.credentials.get("region")
        token = generate_aws_rds_token_with_iam_user(
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            host=host,
            user=user,
            port=port,
            region=region,
        )

        return token

    def get_iam_role_token(self):
        """Get an IAM role token for AWS RDS database authentication.

        This method generates a temporary authentication token for IAM role-based
        authentication with AWS RDS databases. It requires an AWS role ARN and
        database connection details.

        Returns:
            str: A temporary authentication token for database access.

        Raises:
            SqlAwsCredentialsError: If required credentials (aws_role_arn or database) are missing.
        """
        extra = parse_credentials_extra(self.credentials)
        aws_role_arn = extra.get("aws_role_arn")
        database = extra.get("database")
        external_id = extra.get("aws_external_id")

        if not aws_role_arn:
            raise SqlAwsCredentialsError(
                message="aws_role_arn is required for IAM role authentication",
            )
        if not database:
            raise SqlAwsCredentialsError(
                message="database is required for IAM role authentication",
            )

        session_name = AWS_SESSION_NAME
        username = self.credentials.get("username")
        host = self.credentials.get("host")
        port = self.credentials.get("port")
        region = self.credentials.get("region")

        token = generate_aws_rds_token_with_iam_role(
            role_arn=aws_role_arn,
            host=host,
            user=username,
            external_id=external_id,
            session_name=session_name,
            port=port,
            region=region,
        )
        return token

    def get_auth_token(self) -> str:
        """Get the appropriate authentication token based on auth type.

        This method determines the authentication type from credentials and returns
        the corresponding token. Supports basic auth, IAM user, and IAM role
        authentication methods.

        Returns:
            str: URL-encoded authentication token.

        Raises:
            SqlCredentialsParseError: If an invalid authentication type is specified.
        """
        authType = self.credentials.get("authType", "basic")  # Default to basic auth
        token = None

        match authType:
            case "iam_user":
                token = self.get_iam_user_token()
            case "iam_role":
                token = self.get_iam_role_token()
            case "basic":
                token = self.credentials.get("password")
            case _:
                raise SqlCredentialsParseError(field="authType", value_summary=authType)

        # Handle None values and ensure token is a string before encoding
        encoded_token = quote_plus(str(token or ""))
        return encoded_token

    def add_connection_params(
        self, connection_string: str, source_connection_params: dict[str, Any]
    ) -> str:
        """Add additional connection parameters to a SQLAlchemy connection string.

        Args:
            connection_string (str): Base SQLAlchemy connection string.
            source_connection_params (Dict[str, Any]): Additional connection parameters
                to append to the connection string. Keys and values must be
                **raw, not pre-encoded** — both are URL-encoded here (via
                ``quote_plus``) to prevent ``&``/``=`` injection, so a
                pre-encoded value would be double-encoded.

        Returns:
            str: Connection string with additional parameters appended.
        """
        for key, value in source_connection_params.items():
            if "?" not in connection_string:
                connection_string += "?"
            else:
                connection_string += "&"
            # URL-encode so values containing &, =, or spaces can't inject
            # extra connection parameters (e.g. sslmode overrides).
            connection_string += f"{quote_plus(str(key))}={quote_plus(str(value))}"

        return connection_string

    def get_sqlalchemy_connection_string(self) -> str:
        """Generate a SQLAlchemy connection string for database connection.

        This method constructs a connection string using the configured database
        parameters and credentials. It handles different authentication methods
        and includes necessary connection parameters.

        Returns:
            str: Complete SQLAlchemy connection string.

        Raises:
            SqlClientConfigError: If DB_CONFIG is not set.
            MissingSqlParamError: If required connection parameters are missing.
        """
        if not self.DB_CONFIG:
            raise SqlClientConfigError()

        extra = parse_credentials_extra(self.credentials)

        auth_token = self.get_auth_token()

        # Prepare parameters
        param_values = {}
        for param in self.DB_CONFIG.required:
            if param == "password":
                param_values[param] = auth_token
            else:
                value = self.credentials.get(param) or extra.get(param)
                if value is None:
                    raise MissingSqlParamError(field=param)
                param_values[param] = value

        # Fill in base template
        conn_str = self.DB_CONFIG.template.format(**param_values)

        # Append defaults if not already in the template
        if self.DB_CONFIG.defaults:
            conn_str = self.add_connection_params(conn_str, self.DB_CONFIG.defaults)

        if self.DB_CONFIG.parameters:
            parameter_keys = self.DB_CONFIG.parameters
            parameter_values = {
                key: value
                for key in parameter_keys
                if (value := self.credentials.get(key) or extra.get(key)) is not None
            }
            if parameter_values:
                conn_str = self.add_connection_params(conn_str, parameter_values)

        return conn_str

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
            EngineNotInitializedError: If engine is not initialized.
        """
        if not self.engine:
            raise EngineNotInitializedError()

        loop = asyncio.get_running_loop()
        logger.debug(
            "Running query (sha=%s, len=%d)",
            hashlib.sha256(query.encode("utf-8", errors="replace")).hexdigest()[:16],
            len(query),
        )

        # Use context manager for automatic connection cleanup
        with self.engine.connect() as connection:
            if self.use_server_side_cursor:
                connection = connection.execution_options(yield_per=batch_size)

            with ThreadPoolExecutor() as pool:
                from sqlalchemy import text  # noqa: PLC0415 — optional dep: sqlalchemy

                cursor = await loop.run_in_executor(
                    pool, connection.execute, text(query)
                )
                if not cursor or not cursor.cursor:
                    raise UnsupportedSqlCursorError()
                column_names: list[str] = [
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
        self, conn, query, chunksize: int | None
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
        import pandas as pd  # noqa: PLC0415 — optional dep: pandas
        from pandas.compat._optional import (  # noqa: PLC0415 — optional dep: pandas
            import_optional_dependency,
        )
        from sqlalchemy import text  # noqa: PLC0415 — optional dep: sqlalchemy

        if import_optional_dependency("sqlalchemy", errors="ignore"):
            return pd.read_sql_query(text(query), conn, chunksize=chunksize)
        else:
            dbapi_conn = getattr(conn, "connection", None)
            return pd.read_sql_query(query, dbapi_conn, chunksize=chunksize)

    def _read_sql_query(
        self, session: "Session", query: str, chunksize: int | None
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
        self, query: str, chunksize: int | None
    ) -> Union["daft.DataFrame", Iterator["daft.DataFrame"]]:
        """Execute SQL query using the provided engine and daft.

        Returns:
            Union["daft.DataFrame", Iterator["daft.DataFrame"]]: Query results as DataFrame
                or iterator of DataFrames if chunked.
        """
        # Guard must come before the daft import: daft's Rust OTel extension can
        # raise BaseException at import time, which would prevent this check from running.
        if not self.engine:
            raise EngineNotInitializedError()

        # Daft uses ConnectorX to read data from SQL by default for supported connectors
        # If a connection string is passed, it will use ConnectorX to read data
        # For unsupported connectors and if directly engine is passed, it will use SQLAlchemy
        import daft  # noqa: PLC0415 — optional dep: daft

        if isinstance(self.engine, str):
            return daft.read_sql(query, self.engine, infer_schema_length=chunksize)
        return daft.read_sql(query, self.engine.connect, infer_schema_length=chunksize)

    def _execute_query(
        self, query: str, chunksize: int | None
    ) -> Union["pd.DataFrame", Iterator["pd.DataFrame"]]:
        """Execute SQL query using the provided engine and pandas.

        Returns:
            Union["pd.DataFrame", Iterator["pd.DataFrame"]]: Query results as DataFrame
                or iterator of DataFrames if chunked.
        """
        if not self.engine:
            raise EngineNotInitializedError()

        with self.engine.connect() as conn:
            return self._execute_pandas_query(conn, query, chunksize)

    async def _execute_async_read_operation(
        self, query: str, chunksize: int | None
    ) -> Union["pd.DataFrame", Iterator["pd.DataFrame"]]:
        """Helper to execute async read operation with either async session or thread executor."""
        if isinstance(self.engine, str):
            raise InvalidSqlEngineTypeError()

        from sqlalchemy.ext.asyncio import (  # noqa: PLC0415 — optional dep: sqlalchemy
            AsyncEngine,
            AsyncSession,
        )

        async_session = None
        if self.engine and isinstance(self.engine, AsyncEngine):
            from sqlalchemy.orm import (  # noqa: PLC0415 — optional dep: sqlalchemy
                sessionmaker,
            )

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
                return await asyncio.get_running_loop().run_in_executor(
                    executor, self._execute_query, query, chunksize
                )

    async def get_batched_results(
        self,
        query: str,
    ) -> AsyncIterator["pd.DataFrame"] | Iterator["pd.DataFrame"]:  # type: ignore
        """Get query results as batched pandas DataFrames asynchronously.

        Returns:
            AsyncIterator["pd.DataFrame"]: Async iterator yielding batches of query results.

        Raises:
            InvalidSqlEngineTypeError: If engine is a string instead of a SQLAlchemy engine.
            SqlPandasResultError: If there's an error executing the query.
        """
        try:
            # We cast to Iterator because passing chunk_size guarantees an Iterator return
            result = await self._execute_async_read_operation(query, self.chunk_size)
            return cast(Iterator["pd.DataFrame"], result)
        except AppError:
            raise
        # conformance: ignore[E004] exception re-raised immediately as typed SqlPandasResultError; cause chain preserved via `from e`
        except Exception as e:
            raise SqlPandasResultError(
                message="Error reading batched data from SQL", cause=e
            ) from e

    async def get_results(self, query: str) -> "pd.DataFrame":
        """Get all query results as a single pandas DataFrame asynchronously.

        Returns:
            pd.DataFrame: Query results as a DataFrame.

        Raises:
            InvalidSqlEngineTypeError: If engine is a string instead of a SQLAlchemy engine.
            SqlPandasResultError: If there's an error executing the query.
        """
        try:
            result = await self._execute_async_read_operation(query, None)
            import pandas as pd  # noqa: PLC0415 — optional dep: pandas

            if isinstance(result, pd.DataFrame):
                return result
            raise SqlPandasResultError(
                invariant="_execute_async_read_operation must return a pd.DataFrame"
            )
        except AppError:
            raise
        # conformance: ignore[E004] exception re-raised immediately as typed SqlPandasResultError; cause chain preserved via `from e`
        except Exception as e:
            raise SqlPandasResultError(cause=e) from e


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

    async def load(self, credentials: dict[str, Any]) -> None:
        """Load credentials and prepare async engine for lazy connections.

        This method stores credentials and creates an async engine without establishing
        a persistent connection. Connections are created on-demand for better memory efficiency.

        Args:
            credentials (Dict[str, Any]): Database connection credentials including
                host, port, username, password, and other connection parameters.

        Raises:
            SqlClientConfigError: If DB_CONFIG is not set.
            SqlClientAuthFailedError: If credentials are invalid or engine creation fails.
        """
        self.credentials = credentials
        if not self.DB_CONFIG:
            raise SqlClientConfigError()

        try:
            from sqlalchemy.ext.asyncio import (  # noqa: PLC0415 — optional dep: sqlalchemy
                create_async_engine,
            )

            # Create async engine but no persistent connection
            self.engine = create_async_engine(
                self.get_sqlalchemy_connection_string(),
                connect_args=self.DB_CONFIG.connect_args,
                pool_pre_ping=self.DB_CONFIG.pool_pre_ping,
            )

            # Same WARE-970 tolerant-decoder hook as the sync client. Async
            # engines surface DBAPI events on the underlying sync_engine.
            install_tolerant_text_decoder_hook(self.engine.sync_engine)

            # Test connection briefly to validate credentials
            async with self.engine.connect() as _:
                pass  # Connection test successful

            # Don't store persistent connection
            self.connection = None

        # conformance: ignore[E004] exc_info omitted intentionally; SQLAlchemy embeds password in traceback; cause is sanitized and re-raised as typed error
        except Exception as e:
            # No exc_info here: SQLAlchemy errors embed the full connection
            # string (including the password) in their message, and the
            # traceback would print it verbatim into logs.
            logger.error(  # conformance: ignore[E005] exc_info would expose SQLAlchemy password in traceback; cause sanitized above
                "Error establishing database connection: %s", sanitize_cause_repr(e)
            )
            if self.engine:
                await self.engine.dispose()
                self.engine = None
            raise SqlClientAuthFailedError(cause=e) from e

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
            EngineNotInitializedError: If engine is not initialized.
        """
        if not self.engine:
            raise EngineNotInitializedError()

        logger.debug(
            "Running query (sha=%s, len=%d)",
            hashlib.sha256(query.encode("utf-8", errors="replace")).hexdigest()[:16],
            len(query),
        )
        use_server_side_cursor = self.use_server_side_cursor

        # Use async context manager for automatic connection cleanup
        async with self.engine.connect() as connection:
            try:
                from sqlalchemy import text  # noqa: PLC0415 — optional dep: sqlalchemy

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

            except AppError:
                raise
            # conformance: ignore[E004] exception re-raised immediately as typed SqlPandasResultError; cause chain preserved via `from e`
            except Exception as e:
                raise SqlPandasResultError(
                    message="Error executing SQL query", cause=e
                ) from e
            # Async connection automatically closed by context manager

        logger.info("Query execution completed")
