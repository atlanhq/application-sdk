import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from temporalio import activity

from application_sdk.workflows.sql.resources.sql_resource import (
    SQLResource,
    SQLResourceConfig,
)

logger = logging.getLogger(__name__)


class AsyncSQLResource(SQLResource):
    config: SQLResourceConfig
    connection: AsyncConnection | None = None
    engine: AsyncEngine | None = None

    default_database_alias_key = "catalog_name"
    default_schema_alias_key = "schema_name"

    async def load(self):
        self.engine = create_async_engine(
            self.get_sqlalchemy_connection_string(),
            connect_args=self.config.get_sqlalchemy_connect_args(),
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
        use_server_side_cursor = self.config.use_server_side_cursor

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
