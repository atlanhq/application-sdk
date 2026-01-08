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
from temporalio import activity

from application_sdk.clients import ClientInterface
from application_sdk.clients.models import DatabaseConfig
from application_sdk.common.aws_utils import (
    generate_aws_rds_token_with_iam_role,
    generate_aws_rds_token_with_iam_user,
)
from application_sdk.common.error_codes import ClientError, CommonError
from application_sdk.common.utils import parse_credentials_extra
from application_sdk.constants import AWS_SESSION_NAME, USE_SERVER_SIDE_CURSOR
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)
activity.logger = logger

if TYPE_CHECKING:
    import daft
    import pandas as pd
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
    credentials: Dict[str, Any] = {}
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
        self.use_server_side_cursor = use_server_side_cursor
        self.credentials = credentials
        self.chunk_size = chunk_size

    async def load(self, credentials: Dict[str, Any]) -> None:
        """Load credentials and prepare engine for lazy connections.

        This method now only stores credentials and creates the engine without
        establishing a persistent connection. Connections are created on-demand.

        Args:
            credentials (Dict[str, Any]): Database connection credentials.

        Raises:
            ClientError: If credentials are invalid or engine creation fails
        """
        if not self.DB_CONFIG:
            raise ValueError("DB_CONFIG is not configured for this SQL client.")

        self.credentials = credentials  # Update the instance credentials
        try:
            from sqlalchemy import create_engine

            # Create engine but no persistent connection
            self.engine = create_engine(
                self.get_sqlalchemy_connection_string(),
                connect_args=self.DB_CONFIG.connect_args,
                pool_pre_ping=True,
            )

            # Test connection briefly to validate credentials
            with self.engine.connect() as _:
                pass  # Connection test successful

            # Don't store persistent connection
            self.connection = None

        except Exception as e:
            logger.error(
                f"{ClientError.SQL_CLIENT_AUTH_ERROR}: Error loading SQL client: {str(e)}"
            )
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

    def get_iam_user_token(self):
        """Get an IAM user token for AWS RDS database authentication.

        This method generates a temporary authentication token for IAM user-based
        authentication with AWS RDS databases. It requires AWS access credentials
        and database connection details.

        Returns:
            str: A temporary authentication token for database access.

        Raises:
            CommonError: If required credentials (username or database) are missing.
        """
        extra = parse_credentials_extra(self.credentials)
        aws_access_key_id = self.credentials.get("username")
        aws_secret_access_key = self.credentials.get("password")
        host = self.credentials.get("host")
        user = extra.get("username")
        database = extra.get("database")
        if not user:
            raise CommonError(
                f"{CommonError.CREDENTIALS_PARSE_ERROR}: username is required for IAM user authentication"
            )
        if not database:
            raise CommonError(
                f"{CommonError.CREDENTIALS_PARSE_ERROR}: database is required for IAM user authentication"
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
            CommonError: If required credentials (aws_role_arn or database) are missing.
        """
        extra = parse_credentials_extra(self.credentials)
        aws_role_arn = extra.get("aws_role_arn")
        database = extra.get("database")
        external_id = extra.get("aws_external_id")

        if not aws_role_arn:
            raise CommonError(
                f"{CommonError.CREDENTIALS_PARSE_ERROR}: aws_role_arn is required for IAM role authentication"
            )
        if not database:
            raise CommonError(
                f"{CommonError.CREDENTIALS_PARSE_ERROR}: database is required for IAM role authentication"
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
            CommonError: If an invalid authentication type is specified.
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
                raise CommonError(f"{CommonError.CREDENTIALS_PARSE_ERROR}: {authType}")

        # Handle None values and ensure token is a string before encoding
        encoded_token = quote_plus(str(token or ""))
        return encoded_token

    def add_connection_params(
        self, connection_string: str, source_connection_params: Dict[str, Any]
    ) -> str:
        """Add additional connection parameters to a SQLAlchemy connection string.

        Args:
            connection_string (str): Base SQLAlchemy connection string.
            source_connection_params (Dict[str, Any]): Additional connection parameters
                to append to the connection string.

        Returns:
            str: Connection string with additional parameters appended.
        """
        for key, value in source_connection_params.items():
            if "?" not in connection_string:
                connection_string += "?"
            else:
                connection_string += "&"
            connection_string += f"{key}={value}"

        return connection_string

    def get_supported_sqlalchemy_url(self, sqlalchemy_url: str) -> str:
        """Update the dialect in the URL if it is different from the installed dialect.

        Args:
            url (str): The URL to update.

        Returns:
            str: The updated URL with the dialect.
        """
        if not self.DB_CONFIG:
            raise ValueError("DB_CONFIG is not configured for this SQL client.")
        installed_dialect = self.DB_CONFIG.template.split("://")[0]
        url_dialect = sqlalchemy_url.split("://")[0]
        if installed_dialect != url_dialect:
            sqlalchemy_url = sqlalchemy_url.replace(url_dialect, installed_dialect)
        return sqlalchemy_url

    def get_sqlalchemy_connection_string(self) -> str:
        """Generate a SQLAlchemy connection string for database connection.

        This method constructs a connection string using the configured database
        parameters and credentials. It handles different authentication methods
        and includes necessary connection parameters.

        Returns:
            str: Complete SQLAlchemy connection string.

        Raises:
            ValueError: If required connection parameters are missing.
        """
        if not self.DB_CONFIG:
            raise ValueError("DB_CONFIG is not configured for this SQL client.")

        extra = parse_credentials_extra(self.credentials)

        # TODO: Uncomment this when the native deployment is ready
        # If the compiled_url is present, use it directly
        # sqlalchemy_url = extra.get("compiled_url")
        # if sqlalchemy_url:
        #     return self.get_supported_sqlalchemy_url(sqlalchemy_url)

        auth_token = self.get_auth_token()

        # Prepare parameters
        param_values = {}
        for param in self.DB_CONFIG.required:
            if param == "password":
                param_values[param] = auth_token
            else:
                value = self.credentials.get(param) or extra.get(param)
                if value is None:
                    raise ValueError(f"{param} is required")
                param_values[param] = value

        # Fill in base template
        conn_str = self.DB_CONFIG.template.format(**param_values)

        # Append defaults if not already in the template
        if self.DB_CONFIG.defaults:
            conn_str = self.add_connection_params(conn_str, self.DB_CONFIG.defaults)

        if self.DB_CONFIG.parameters:
            parameter_keys = self.DB_CONFIG.parameters
            parameter_values = {
                key: self.credentials.get(key) or extra.get(key)
                for key in parameter_keys
            }
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
            ValueError: If engine is not initialized.
            Exception: If query execution fails.
        """
        if not self.engine:
            raise ValueError("Engine is not initialized. Call load() first.")

        loop = asyncio.get_running_loop()
        logger.info(f"Running query: {query}")

        # Use context manager for automatic connection cleanup
        with self.engine.connect() as connection:
            if self.use_server_side_cursor:
                connection = connection.execution_options(yield_per=batch_size)

            with ThreadPoolExecutor() as pool:
                try:
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
                except Exception as e:
                    logger.error("Error running query in batch: {error}", error=str(e))
                    raise e
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
            logger.error(f"Error reading batched data(pandas) from SQL: {str(e)}")

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
            logger.error(f"Error reading data(pandas) from SQL: {str(e)}")
            raise e


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
        """Load credentials and prepare async engine for lazy connections.

        This method stores credentials and creates an async engine without establishing
        a persistent connection. Connections are created on-demand for better memory efficiency.

        Args:
            credentials (Dict[str, Any]): Database connection credentials including
                host, port, username, password, and other connection parameters.

        Raises:
            ValueError: If credentials are invalid or engine creation fails.
        """
        self.credentials = credentials
        if not self.DB_CONFIG:
            raise ValueError("DB_CONFIG is not configured for this SQL client.")

        try:
            from sqlalchemy.ext.asyncio import create_async_engine

            # Create async engine but no persistent connection
            self.engine = create_async_engine(
                self.get_sqlalchemy_connection_string(),
                connect_args=self.DB_CONFIG.connect_args,
                pool_pre_ping=True,
            )
            if not self.engine:
                raise ValueError("Failed to create async engine")

            # Test connection briefly to validate credentials
            async with self.engine.connect() as _:
                pass  # Connection test successful

            # Don't store persistent connection
            self.connection = None

        except Exception as e:
            logger.error(f"Error establishing database connection: {str(e)}")
            if self.engine:
                await self.engine.dispose()
                self.engine = None
            raise ValueError(str(e))

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

        logger.info(f"Running query: {query}")
        use_server_side_cursor = self.use_server_side_cursor

        # Use async context manager for automatic connection cleanup
        async with self.engine.connect() as connection:
            try:
                from sqlalchemy import text

                if use_server_side_cursor:
                    connection = connection.execution_options(yield_per=batch_size)

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
                logger.error(f"Error executing query: {str(e)}")
                raise
            # Async connection automatically closed by context manager

        logger.info("Query execution completed")
