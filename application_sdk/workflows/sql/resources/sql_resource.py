import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List

from sqlalchemy import create_engine, text
from temporalio import activity

from application_sdk import activity_pd
from application_sdk.inputs.sql_query import SQLQueryInput
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


class SQLResource(ResourceInterface):
    config: SQLResourceConfig
    connection = None
    engine = None
    sql_input = SQLQueryInput

    default_database_alias_key = "catalog_name"
    default_schema_alias_key = "schema_name"

    def __init__(self, config: SQLResourceConfig | None = None):
        if config is None:
            raise ValueError("config is required")

        self.config = config

        super().__init__()

    async def load(self):
        self.engine = create_engine(
            self.get_sqlalchemy_connection_string(),
            connect_args=self.config.get_sqlalchemy_connect_args(),
            pool_pre_ping=True,
        )
        self.connection = self.engine.connect()

    def set_credentials(self, credentials: Dict[str, Any]) -> None:
        self.config.set_credentials(credentials)

    def get_sqlalchemy_connection_string(self) -> str:
        raise NotImplementedError("get_sqlalchemy_connection_string is not implemented")

    @activity_pd(
        batch_input=lambda self, args: self.sql_input(
            self.engine,
            args["metadata_sql"],
        )
    )
    async def fetch_metadata(
        self,
        batch_input: str,
        metadata_sql: str,
        database_alias_key: str | None = None,
        schema_alias_key: str | None = None,
        database_result_key: str = "TABLE_CATALOG",
        schema_result_key: str = "TABLE_SCHEMA",
    ):
        if database_alias_key is None:
            database_alias_key = self.default_database_alias_key

        if schema_alias_key is None:
            schema_alias_key = self.default_schema_alias_key

        result: List[Dict[Any, Any]] = []
        try:
            for row in batch_input.to_dict(orient="records"):
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
