"""
SQL client implementation for database connections.

This module provides SQL client classes for both synchronous and asynchronous
database operations, supporting batch processing and server-side cursors.
"""

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List
from urllib.parse import quote_plus, urlencode

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from temporalio import activity

from application_sdk.clients import ClientInterface
from application_sdk.common.aws_utils import (
    generate_aws_rds_token_with_iam_role,
    generate_aws_rds_token_with_iam_user,
)
from application_sdk.common.logger_adaptors import get_logger
from application_sdk.common.utils import parse_credentials_extra
from application_sdk.constants import USE_SERVER_SIDE_CURSOR

activity.logger = get_logger(__name__)


class BaseSQLClient(ClientInterface):
    """SQL client for database operations.

    This class provides functionality for connecting to and querying SQL databases,
    with support for batch processing and server-side cursors.

    Attributes:
        connection: Database connection instance.
        engine: SQLAlchemy engine instance.
        sql_alchemy_connect_args (Dict[str, Any]): Additional connection arguments.
        credentials (Dict[str, Any]): Database credentials.
        use_server_side_cursor (bool): Whether to use server-side cursors.
    """

    connection = None
    engine = None
    sql_alchemy_connect_args: Dict[str, Any] = {}
    credentials: Dict[str, Any] = {}
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
        self.sql_alchemy_connect_args = sql_alchemy_connect_args

    async def load(self, credentials: Dict[str, Any]) -> None:
        """Load and establish the database connection.

        Args:
            credentials (Dict[str, Any]): Database connection credentials.

        Raises:
            ValueError: If connection fails due to authentication or connection issues
        """
        self.credentials = credentials
        try:
            from sqlalchemy import create_engine

            self.engine = create_engine(
                self.get_sqlalchemy_connection_string(),
                connect_args=self.sql_alchemy_connect_args,
                pool_pre_ping=True,
            )
            self.connection = self.engine.connect()
        except Exception as e:
            activity.logger.error(f"Error loading SQL client: {str(e)}")
            if self.engine:
                self.engine.dispose()
                self.engine = None
            raise ValueError(str(e))

    async def close(self) -> None:
        """Close the database connection."""
        if self.connection:
            self.connection.close()

    def get_iam_user_token(self):
        """
        Get the IAM user token for the database.
        This is a temporary token that is used to authenticate the IAM user to the database.
        """
        extra = parse_credentials_extra(self.credentials)
        aws_access_key_id = self.credentials["username"]
        aws_secret_access_key = self.credentials["password"]
        host = self.credentials["host"]
        user = extra.get("username")
        database = extra.get("database")
        if not user:
            raise ValueError("username is required for IAM user authentication")
        if not database:
            raise ValueError("database is required for IAM user authentication")

        port = self.credentials["port"]
        region = self.credentials.get("region", None)
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
        """
        Get the IAM role token for the database.
        This is a temporary token that is used to authenticate the IAM role to the database.
        """
        extra = parse_credentials_extra(self.credentials)
        aws_role_arn = extra.get("aws_role_arn")
        database = extra.get("database")
        external_id = extra.get("aws_external_id")

        if not aws_role_arn:
            raise ValueError("aws_role_arn is required for IAM role authentication")
        if not database:
            raise ValueError("database is required for IAM role authentication")

        session_name = os.getenv("AWS_SESSION_NAME", "temp-session")
        username = self.credentials["username"]
        host = self.credentials["host"]
        port = self.credentials.get("port", 5432)
        region = self.credentials.get("region", None)
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
        """
        Get the auth token for the SQL source.
        """
        authType = self.credentials.get("authType", "basic")  # Default to basic auth
        token = None

        match authType:
            case "iam_user":
                token = self.get_iam_user_token()
            case "iam_role":
                token = self.get_iam_role_token()
            case "basic":
                token = self.credentials["password"]
            case _:
                raise ValueError(f"Invalid auth type: {authType}")

        encoded_token = quote_plus(token)
        return encoded_token
    
    def add_connection_params(
        self, connection_string: str, source_connection_params: Dict[str, Any]
    ) -> str:
        """
        Add the source connection params to the connection string.
        """
        for key, value in source_connection_params.items():
            if "?" not in connection_string:
                connection_string += "?"
            else:
                connection_string += "&"
            connection_string += f"{key}={value}"

        return connection_string
    
    def get_sqlalchemy_connection_string(self) -> str:
        """
        Get the SQLAlchemy connection string for database connection.

        This method constructs the connection string using the configured database parameters
        and authentication token. It handles both basic authentication and IAM-based auth.

        The connection string is built using:
        1. Required parameters from DB_CONFIG["required"] list
        2. Authentication token from get_auth_token() as the password
        3. Default connection parameters from DB_CONFIG["defaults"] dict

        The DB_CONFIG structure should be defined as:
        {
            "template": "postgresql+psycopg2://{username}:{password}@{host}:{port}/{database}",
            "required": ["username", "password", "host", "port", "database"],
            "defaults": {"connect_timeout": 5}
        }

        Returns:
            str: The complete SQLAlchemy connection string with all required parameters
                and authentication details. For example:
                "postgresql+psycopg2://user:pass@localhost:5432/mydb?connect_timeout=5"

        Note:
            The password parameter in the connection string will be replaced with the
            authentication token obtained from get_auth_token(), which supports both
            basic auth and IAM-based authentication methods.
        """
        extra = parse_credentials_extra(self.credentials)
        auth_token = self.get_auth_token()

        # Prepare parameters
        param_values = {}
        for param in self.DB_CONFIG["required"]:
            if param == "password":
                param_values[param] = auth_token
            else:
                param_values[param] = self.credentials.get(param) or extra.get(param)

        # Fill in base template
        conn_str = self.DB_CONFIG["template"].format(**param_values)

        # Append defaults if not already in the template
        if self.DB_CONFIG.get("defaults"):
            conn_str = self.add_connection_params(conn_str, self.DB_CONFIG["defaults"])

        if self.DB_CONFIG.get("parameters"):
            parameter_keys = self.DB_CONFIG["parameters"]
            self.DB_CONFIG["parameters"] = {
                key: self.credentials.get(key) or extra.get(key) for key in parameter_keys
            }
            conn_str = self.add_connection_params(conn_str, self.DB_CONFIG["parameters"])

        return conn_str

    async def run_query(self, query: str, batch_size: int = 100000):
        """
        Run a query in batch mode with client-side cursor.

        This method also supports server-side cursor via sqlalchemy execution options(yield_per=batch_size).
        If yield_per is not supported by the database, the method will fall back to client-side cursor.

        Args:
            query: The query to run.
            batch_size: The batch size.

        Yields:
            List of dictionaries containing query results.

        Raises:
            ValueError: If connection is not established.
            Exception: If the query fails.
        """
        if not self.connection:
            raise ValueError("Connection is not established")
        loop = asyncio.get_running_loop()

        if self.use_server_side_cursor:
            self.connection.execution_options(yield_per=batch_size)

        activity.logger.info("Running query: {query}", query=query)

        with ThreadPoolExecutor() as pool:
            try:
                from sqlalchemy import text

                cursor = await loop.run_in_executor(
                    pool, self.connection.execute, text(query)
                )
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
                activity.logger.error(
                    "Error running query in batch: {error}", error=str(e)
                )
                raise e

        activity.logger.info("Query execution completed")


class AsyncBaseSQLClient(BaseSQLClient):
    """Asynchronous SQL client for database operations.

    This class extends BaseSQLClient to provide asynchronous database operations,
    with support for batch processing and server-side cursors.

    Attributes:
        connection (AsyncConnection | None): Async database connection instance.
        engine (AsyncEngine | None): Async SQLAlchemy engine instance.
    """

    connection: "AsyncConnection"
    engine: "AsyncEngine"

    async def load(self, credentials: Dict[str, Any]) -> None:
        """Load and establish an asynchronous database connection.

        Args:
            credentials (Dict[str, Any]): Database connection credentials.

        Raises:
            ValueError: If connection fails due to authentication or connection issues
        """
        self.credentials = credentials
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
            activity.logger.error(f"Error establishing database connection: {str(e)}")
            if self.engine:
                await self.engine.dispose()
                self.engine = None
            raise ValueError(str(e))

    async def run_query(self, query: str, batch_size: int = 100000):
        """
        Run a query in batch mode with client-side cursor.

        This method also supports server-side cursor via sqlalchemy execution options(yield_per=batch_size).
        If yield_per is not supported by the database, the method will fall back to client-side cursor.

        Args:
            query: The query to run.
            batch_size: The batch size.

        Yields:
            List of dictionaries containing query results.

        Raises:
            ValueError: If connection is not established.
            Exception: If the query fails.
        """
        if not self.connection:
            raise ValueError("Connection is not established")

        activity.logger.info("Running query: {query}", query=query)
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
                )
                if not rows:
                    break
                yield [dict(zip(column_names, row)) for row in rows]

        except Exception as e:
            activity.logger.error("Error executing query: {error}", error=str(e))
            raise

        activity.logger.info("Query execution completed")
