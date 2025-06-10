"""
SQL client implementation for database connections.

This module provides SQL client classes for both synchronous and asynchronous
database operations, supporting batch processing and server-side cursors.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List
from urllib.parse import quote_plus

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from temporalio import activity

from application_sdk.clients import ClientInterface
from application_sdk.common.aws_utils import (
    generate_aws_rds_token_with_iam_role,
    generate_aws_rds_token_with_iam_user,
)
from application_sdk.common.credential_utils import resolve_credentials
from application_sdk.common.error_codes import ClientError, CommonError
from application_sdk.common.utils import parse_credentials_extra
from application_sdk.constants import AWS_SESSION_NAME, USE_SERVER_SIDE_CURSOR
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)
activity.logger = logger


class BaseSQLClient(ClientInterface):
    """SQL client for database operations.

    This class provides functionality for connecting to and querying SQL databases,
    with support for batch processing and server-side cursors.

    Attributes:
        connection: Database connection instance.
        engine: SQLAlchemy engine instance.
        sql_alchemy_connect_args (Dict[str, Any]): Additional connection arguments.
        credentials (Dict[str, Any]): Database credentials.
        resolved_credentials (Dict[str, Any]): Resolved credentials after reading from secret manager.
        use_server_side_cursor (bool): Whether to use server-side cursors.
    """

    connection = None
    engine = None
    sql_alchemy_connect_args: Dict[str, Any] = {}
    credentials: Dict[str, Any] = {}
    resolved_credentials: Dict[str, Any] = {}
    use_server_side_cursor: bool = USE_SERVER_SIDE_CURSOR
    DB_CONFIG: Dict[str, Any] = {}

    def __init__(
        self,
        use_server_side_cursor: bool = USE_SERVER_SIDE_CURSOR,
        credentials: Dict[str, Any] = {},
        sql_alchemy_connect_args: Dict[str, Any] = {},
    ):
        """
        Initialize the SQL client.

        Args:
            use_server_side_cursor (bool, optional): Whether to use server-side cursors.
                Defaults to USE_SERVER_SIDE_CURSOR.
            credentials (Dict[str, Any], optional): Database credentials. Defaults to {}.
            sql_alchemy_connect_args (Dict[str, Any], optional): Additional SQLAlchemy
                connection arguments. Defaults to {}.
        """
        self.use_server_side_cursor = use_server_side_cursor
        self.credentials = credentials
        self.resolved_credentials = {}
        self.sql_alchemy_connect_args = sql_alchemy_connect_args

    async def load(self, credentials: Dict[str, Any]) -> None:
        """Load and establish the database connection.

        Args:
            credentials (Dict[str, Any]): Database connection credentials.

        Raises:
            ClientError: If connection fails due to authentication or connection issues
        """
        self.credentials = credentials  # Update the instance credentials
        self.resolved_credentials = await resolve_credentials(credentials)
        try:
            from sqlalchemy import create_engine

            self.engine = create_engine(
                self.get_sqlalchemy_connection_string(),
                connect_args=self.sql_alchemy_connect_args,
                pool_pre_ping=True,
            )
            self.connection = self.engine.connect()
        except ClientError as e:
            logger.error(
                f"{ClientError.SQL_CLIENT_AUTH_ERROR}: Error loading SQL client: {str(e)}"
            )
            if self.engine:
                self.engine.dispose()
                self.engine = None
            raise ClientError(f"{ClientError.SQL_CLIENT_AUTH_ERROR}: {str(e)}")

    async def close(self) -> None:
        """Close the database connection."""
        if self.connection:
            self.connection.close()

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
        extra = parse_credentials_extra(self.resolved_credentials)
        aws_access_key_id = self.resolved_credentials.get("username")
        aws_secret_access_key = self.resolved_credentials.get("password")
        host = self.resolved_credentials.get("host")
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

        port = self.resolved_credentials.get("port")
        region = self.resolved_credentials.get("region")
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
        extra = parse_credentials_extra(self.resolved_credentials)
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
        username = self.resolved_credentials.get("username")
        host = self.resolved_credentials.get("host")
        port = self.resolved_credentials.get("port")
        region = self.resolved_credentials.get("region")

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
        authType = self.resolved_credentials.get(
            "authType", "basic"
        )  # Default to basic auth
        token = None

        match authType:
            case "iam_user":
                token = self.get_iam_user_token()
            case "iam_role":
                token = self.get_iam_role_token()
            case "basic":
                token = self.resolved_credentials.get("password")
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
        installed_dialect = self.DB_CONFIG["template"].split("://")[0]
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
        extra = parse_credentials_extra(self.resolved_credentials)

        # TODO: Uncomment this when the native deployment is ready
        # If the compiled_url is present, use it directly
        # sqlalchemy_url = extra.get("compiled_url")
        # if sqlalchemy_url:
        #     return self.get_supported_sqlalchemy_url(sqlalchemy_url)

        auth_token = self.get_auth_token()

        # Prepare parameters
        param_values = {}
        for param in self.DB_CONFIG["required"]:
            if param == "password":
                param_values[param] = auth_token
            else:
                value = self.resolved_credentials.get(param) or extra.get(param)
                if value is None:
                    raise ValueError(f"{param} is required")
                param_values[param] = value

        # Fill in base template
        conn_str = self.DB_CONFIG["template"].format(**param_values)

        # Append defaults if not already in the template
        if self.DB_CONFIG.get("defaults"):
            conn_str = self.add_connection_params(conn_str, self.DB_CONFIG["defaults"])

        if self.DB_CONFIG.get("parameters"):
            parameter_keys = self.DB_CONFIG["parameters"]
            self.DB_CONFIG["parameters"] = {
                key: self.resolved_credentials.get(key) or extra.get(key)
                for key in parameter_keys
            }
            conn_str = self.add_connection_params(
                conn_str, self.DB_CONFIG["parameters"]
            )

        return conn_str

    async def run_query(self, query: str, batch_size: int = 100000):
        """Execute a SQL query and return results in batches.

        This method executes the provided SQL query and yields results in batches
        to efficiently manage memory usage for large result sets. It supports both
        server-side and client-side cursors based on configuration.

        Args:
            query (str): SQL query to execute.
            batch_size (int, optional): Number of records to fetch in each batch.
                Defaults to 100000.

        Yields:
            List[Dict[str, Any]]: Batches of query results, where each result is
                a dictionary mapping column names to values.

        Raises:
            ValueError: If database connection is not established.
            Exception: If query execution fails.
        """
        if not self.connection:
            raise ValueError("Connection is not established")
        loop = asyncio.get_running_loop()

        if self.use_server_side_cursor:
            self.connection.execution_options(yield_per=batch_size)

        logger.info(f"Running query: {query}")

        with ThreadPoolExecutor() as pool:
            try:
                from sqlalchemy import text

                cursor = await loop.run_in_executor(
                    pool, self.connection.execute, text(query)
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

        logger.info("Query execution completed")


class AsyncBaseSQLClient(BaseSQLClient):
    """Asynchronous SQL client for database operations.

    This class extends BaseSQLClient to provide asynchronous database operations,
    with support for batch processing and server-side cursors. It uses SQLAlchemy's
    async engine and connection interfaces for non-blocking database operations.

    Attributes:
        connection (AsyncConnection): Async database connection instance.
        engine (AsyncEngine): Async SQLAlchemy engine instance.
        sql_alchemy_connect_args (Dict[str, Any]): Additional connection arguments.
        credentials (Dict[str, Any]): Database credentials.
        use_server_side_cursor (bool): Whether to use server-side cursors.
    """

    connection: "AsyncConnection"
    engine: "AsyncEngine"

    async def load(self, credentials: Dict[str, Any]) -> None:
        """Load and establish an asynchronous database connection.

        This method creates an async SQLAlchemy engine and establishes a connection
        to the database using the provided credentials.

        Args:
            credentials (Dict[str, Any]): Database connection credentials including
                host, port, username, password, and other connection parameters.

        Raises:
            ValueError: If connection fails due to invalid credentials or connection issues.
        """
        self.credentials = credentials
        self.resolved_credentials = await resolve_credentials(credentials)

        try:
            from sqlalchemy.ext.asyncio import create_async_engine

            self.engine = create_async_engine(
                self.get_sqlalchemy_connection_string(),
                connect_args=self.sql_alchemy_connect_args,
                pool_pre_ping=True,
            )
            if not self.engine:
                raise ValueError("Failed to create async engine")
            self.connection = await self.engine.connect()
        except Exception as e:
            logger.error(f"Error establishing database connection: {str(e)}")
            if self.engine:
                await self.engine.dispose()
                self.engine = None
            raise ValueError(str(e))

    async def run_query(self, query: str, batch_size: int = 100000):
        """Execute a SQL query asynchronously and return results in batches.

        This method executes the provided SQL query using an async connection and
        yields results in batches to manage memory usage for large result sets.

        Args:
            query (str): SQL query to execute.
            batch_size (int, optional): Number of records to fetch in each batch.
                Defaults to 100000.

        Yields:
            List[Dict[str, Any]]: Batches of query results, where each result is
                a dictionary mapping column names to values.

        Raises:
            Exception: If query execution fails.
        """
        if not self.connection:
            raise ValueError("Connection is not established")

        logger.info(f"Running query: {query}")
        use_server_side_cursor = self.use_server_side_cursor

        try:
            from sqlalchemy import text

            if use_server_side_cursor:
                await self.connection.execution_options(yield_per=batch_size)

            result = (
                await self.connection.stream(text(query))
                if use_server_side_cursor
                else await self.connection.execute(text(query))
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

        logger.info("Query execution completed")
