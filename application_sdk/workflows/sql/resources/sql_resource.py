import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List
from urllib.parse import quote_plus

from sqlalchemy import create_engine, text
from temporalio import activity

from application_sdk.workflows.resources.temporal_resource import ResourceInterface

logger = logging.getLogger(__name__)


class SQLResourceConfig:
    use_server_side_cursor: bool = True
    credentials: Dict[str, Any] = None
    sql_alchemy_connect_args: Dict[str, Any] = None

    def __init__(
        self,
        use_server_side_cursor: bool = True,
        credentials: Dict[str, Any] = None,
        sql_alchemy_connect_args: Dict[str, Any] = {},
    ):
        self.use_server_side_cursor = use_server_side_cursor
        self.credentials = credentials
        self.sql_alchemy_connect_args = sql_alchemy_connect_args

    def set_credentials(self, credentials: Dict[str, Any]):
        self.credentials = credentials

    def get_sqlalchemy_connect_args(self) -> Dict[str, Any]:
        return self.sql_alchemy_connect_args

    def get_sqlalchemy_connection_string(self) -> str:
        encoded_password = quote_plus(self.credentials["password"])
        return f"postgresql+psycopg2://{self.credentials['user']}:{encoded_password}@{self.credentials['host']}:{self.credentials['port']}/{self.credentials['database']}"


class SQLResource(ResourceInterface):
    config: SQLResourceConfig = None
    connection = None
    engine = None

    default_database_alias_key = "catalog_name"
    default_schema_alias_key = "schema_name"

    def __init__(self, config: SQLResourceConfig = None):
        self.config = config

        super().__init__()

    async def load(self):
        self.engine = create_engine(
            self.config.get_sqlalchemy_connection_string(),
            connect_args=self.config.get_sqlalchemy_connect_args(),
            pool_pre_ping=True,
        )
        self.connection = self.engine.connect()

    def set_credentials(self, credentials):
        self.config.set_credentials(credentials)

    async def fetch_metadata(
        self,
        metadata_sql: str,
        database_alias_key: str = default_database_alias_key,
        schema_alias_key: str = default_schema_alias_key,
        database_result_key: str = "TABLE_CATALOG",
        schema_result_key: str = "TABLE_SCHEMA",
    ):
        result: List[Dict[Any, Any]] = []
        try:
            async for batch in self.run_query(metadata_sql):
                for row in batch:
                    result.append(
                        {
                            database_result_key: row[database_alias_key],
                            schema_result_key: row[schema_alias_key],
                        }
                    )

        except Exception as e:
            logger.error(f"Failed to fetch metadata: {str(e)}")
            raise e

        return result

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

        if self.config.use_server_side_cursor:
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
                        column_names = [str(field) for field in rows[0]._fields]

                    results = [dict(zip(column_names, row)) for row in rows]
                    yield results
            except Exception as e:
                logger.error(f"Error running query in batch: {e}")
                raise e

        activity.logger.info("Query execution completed")
