"""
SQL client implementation for database connections.

This module provides SQL client classes for both synchronous and asynchronous
database operations, supporting batch processing and server-side cursors.
"""

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from typing import Any, Dict, List

from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from temporalio import activity

from application_sdk.clients import ClientInterface
from application_sdk.common.logger_adaptors import get_logger

activity.logger = get_logger(__name__)


class SQLConstants(Enum):
    USE_SERVER_SIDE_CURSOR = bool(os.getenv("ATLAN_SQL_USE_SERVER_SIDE_CURSOR", "true"))


class SQLClient(ClientInterface):
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
    use_server_side_cursor: bool = SQLConstants.USE_SERVER_SIDE_CURSOR.value

    def __init__(
        self,
        use_server_side_cursor: bool = SQLConstants.USE_SERVER_SIDE_CURSOR.value,
        credentials: Dict[str, Any] = {},
        sql_alchemy_connect_args: Dict[str, Any] = {},
    ):
        """
        Initialize the SQL client.

        Args:
            use_server_side_cursor (bool, optional): Whether to use server-side cursors.
                Defaults to SQLConstants.USE_SERVER_SIDE_CURSOR.value.
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
        """
        self.credentials = credentials
        self.engine = create_engine(
            self.get_sqlalchemy_connection_string(),
            connect_args=self.sql_alchemy_connect_args,
            pool_pre_ping=True,
        )
        self.connection = self.engine.connect()

    async def close(self) -> None:
        """Close the database connection."""
        if self.connection:
            self.connection.close()

    def get_sqlalchemy_connection_string(self) -> str:
        raise NotImplementedError("get_sqlalchemy_connection_string is not implemented")
        """Get the SQLAlchemy connection string."""

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
            Exception: If the query fails.
        """
        loop = asyncio.get_running_loop()

        if self.use_server_side_cursor:
            self.connection.execution_options(yield_per=batch_size)

        activity.logger.info("Running query: {query}", query=query)

        with ThreadPoolExecutor() as pool:
            try:
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
                activity.logger.error("Error running query in batch: {error}", error=str(e))
                raise e

        activity.logger.info("Query execution completed")


class AsyncSQLClient(SQLClient):
    """Asynchronous SQL client for database operations.

    This class extends SQLClient to provide asynchronous database operations,
    with support for batch processing and server-side cursors.

    Attributes:
        connection (AsyncConnection | None): Async database connection instance.
        engine (AsyncEngine | None): Async SQLAlchemy engine instance.
    """

    connection: AsyncConnection | None = None
    engine: AsyncEngine | None = None

    async def load(self, credentials: Dict[str, Any]) -> None:
        """Load and establish an asynchronous database connection.

        Args:
            credentials (Dict[str, Any]): Database connection credentials.
        """
        self.credentials = credentials
        self.engine = create_async_engine(
            self.get_sqlalchemy_connection_string(),
            connect_args=self.sql_alchemy_connect_args,
            pool_pre_ping=True,
        )
        self.connection = await self.engine.connect()

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
