import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List
from urllib.parse import quote_plus

from sqlalchemy import create_engine, text
from temporalio import activity

from application_sdk.workflows.resources import Resource

logger = logging.getLogger(__name__)


class SQLResource(Resource):
    use_server_side_cursor: bool = True

    def __init__(self, credentials: Dict[str, Any]):
        self.credentials = credentials
        self.engine = create_engine(
            self.get_sqlalchemy_connection_string(),
            connect_args=self.get_sqlalchemy_connect_args(),
            pool_pre_ping=True,
        )
        self.connection = None

        super().__init__()

    async def load(self):
        self.connection = self.engine.connect()

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
                column_names: List[str] = []

                while True:
                    rows = await loop.run_in_executor(
                        pool, cursor.fetchmany, batch_size
                    )
                    if not rows:
                        break

                    if not column_names:
                        column_names = rows[0]._fields

                    results = [dict(zip(column_names, row)) for row in rows]
                    yield results
            except Exception as e:
                logger.error(f"Error running query in batch: {e}")
                raise e

        activity.logger.info("Query execution completed")

    def get_sqlalchemy_connect_args(self) -> Dict[str, Any]:
        return {}

    def get_sqlalchemy_connection_string(self) -> str:
        encoded_password = quote_plus(self.credentials["password"])
        return f"postgresql+psycopg2://{self.credentials['user']}:{encoded_password}@{self.credentials['host']}:{self.credentials['port']}/{self.credentials['database']}"
