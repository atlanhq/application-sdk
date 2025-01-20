import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List

from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from temporalio import activity

from application_sdk.clients import ClientInterface
from application_sdk.clients.constants import SQLConstants
from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.inputs.sql_query import AsyncSQLQueryInput, SQLQueryInput

logger = AtlanLoggerAdapter(logging.getLogger(__name__))


class SQLClient(ClientInterface):
    connection = None
    engine = None
    sql_input = SQLQueryInput

    default_database_alias_key = SQLConstants.DEFAULT_DATABASE_ALIAS_KEY.value
    default_schema_alias_key = SQLConstants.DEFAULT_SCHEMA_ALIAS_KEY.value

    sql_alchemy_connect_args: Dict[str, Any] = {}
    credentials: Dict[str, Any] = {}
    use_server_side_cursor: bool = SQLConstants.USE_SERVER_SIDE_CURSOR.value

    def __init__(
        self,
        use_server_side_cursor: bool = SQLConstants.USE_SERVER_SIDE_CURSOR.value,
        credentials: Dict[str, Any] = {},
        sql_alchemy_connect_args: Dict[str, Any] = {},
    ):
        self.use_server_side_cursor = use_server_side_cursor
        self.credentials = credentials
        self.sql_alchemy_connect_args = sql_alchemy_connect_args

    async def load(self, credentials: Dict[str, Any]):
        self.credentials = credentials
        self.engine = create_engine(
            self.get_sqlalchemy_connection_string(),
            connect_args=self.sql_alchemy_connect_args,
            pool_pre_ping=True,
        )
        self.connection = self.engine.connect()

    async def close(self):
        if self.connection:
            self.connection.close()

    def get_sqlalchemy_connection_string(self) -> str:
        raise NotImplementedError("get_sqlalchemy_connection_string is not implemented")

    async def run_query(self, query: str, batch_size: int = 100000):
        """
        Run a query in a batch mode with client-side cursor.

        This method also supports server-side cursor via sqlalchemy execution options(yield_per=batch_size)
        If yield_per is not supported by the database, the method will fall back to client-side cursor.

        :param query: The query to run.
        :param batch_size: The batch size.
        :return: The query results.
        :raises Exception: If the query fails.
        """
        loop = asyncio.get_running_loop()

        if self.use_server_side_cursor:
            self.connection.execution_options(yield_per=batch_size)

        activity.logger.info(f"Running query: {query}")

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
                logger.error(f"Error running query in batch: {e}")
                raise e

        activity.logger.info("Query execution completed")


class AsyncSQLClient(SQLClient):
    connection: AsyncConnection | None = None
    engine: AsyncEngine | None = None
    sql_input = AsyncSQLQueryInput

    default_database_alias_key = SQLConstants.DEFAULT_DATABASE_ALIAS_KEY.value
    default_schema_alias_key = SQLConstants.DEFAULT_SCHEMA_ALIAS_KEY.value

    async def load(self, credentials: Dict[str, Any]):
        self.credentials = credentials
        self.engine = create_async_engine(
            self.get_sqlalchemy_connection_string(),
            connect_args=self.sql_alchemy_connect_args,
            pool_pre_ping=True,
        )
        self.connection = await self.engine.connect()

    async def run_query(self, query: str, batch_size: int = 100000):
        """
        Run a query in a batch mode with client-side cursor.

        This method also supports server-side cursor via sqlalchemy execution options(yield_per=batch_size)
        If yield_per is not supported by the database, the method will fall back to client-side cursor.

        :param query: The query to run.
        :param batch_size: The batch size.
        :return: The query results.
        :raises Exception: If the query fails.
        """
        if not self.connection:
            raise ValueError("Connection is not established")

        activity.logger.info(f"Running query: {query}")
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
            logger.error(f"Error executing query: {e}", exc_info=True)
            raise

        logger.info("Query execution completed.")

        activity.logger.info("Query execution completed")
