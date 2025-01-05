import asyncio
import contextvars
import os
import logging
import itertools
from typing import Any, Dict, List
import pandas as pd

from application_sdk.workflows.controllers import WorkflowMetadataControllerInterface
from application_sdk.workflows.sql.resources.sql_resource import SQLResource

logger = logging.getLogger(__name__)


class SQLWorkflowMetadataController(WorkflowMetadataControllerInterface):
    """
    SQL Workflow Metadata Interface

    This interface is used to fetch metadata from the database.

    Attributes:
        METADATA_SQL (str): The SQL query to fetch the metadata.
        DATABASE_KEY (str): The key to fetch the database name.
        SCHEMA_KEY (str): The key to fetch the schema name.

    Usage:
        Subclass this interface and implement the required attributes and any methods
        that need custom behavior (ex. fetch_metadata).

        >>> class MySQLWorkflowMetadataInterface(SQLWorkflowMetadataInterface):
        >>>     METADATA_SQL = "SELECT * FROM information_schema.schemata"
        >>>     DATABASE_KEY = "TABLE_CATALOG"
        >>>     SCHEMA_KEY = "TABLE_SCHEMA"
        >>>     def __init__(self, create_engine_fn: Callable[[Dict[str, Any]], Engine]):
        >>>         super().__init__(create_engine_fn)
    """

    METADATA_SQL: str = ""

    DATABASE_ALIAS_KEY: str | None = None
    SCHEMA_ALIAS_KEY: str | None = None

    DATABASE_KEY: str = "TABLE_CATALOG"
    SCHEMA_KEY: str = "TABLE_SCHEMA"

    sql_resource: SQLResource | None = None

    def __init__(self, sql_resource: SQLResource | None = None):
        self.sql_resource = sql_resource

    async def prepare(self, credentials: Dict[str, Any]) -> None:
        self.sql_resource.set_credentials(credentials)
        await self.sql_resource.load()

    async def fetch_metadata(self) -> List[Dict[str, str]]:
        if not self.sql_resource:
            raise ValueError("SQL Resource not defined")

        args = {
            "metadata_sql": self.METADATA_SQL,
            "database_alias_key": self.DATABASE_ALIAS_KEY,
            "schema_alias_key": self.SCHEMA_ALIAS_KEY,
            "database_result_key": self.DATABASE_KEY,
            "schema_result_key": self.SCHEMA_KEY,
        }
        return await self.sql_resource.fetch_metadata(args)


class SQLDatabaseWorkflowMetadataController(SQLWorkflowMetadataController):
    """
    SQL Database Workflow Metadata Interface

    This interface is used to fetch metadata from the database.

    Attributes:
        METADATA_SQL (str): The SQL query to fetch the metadata.
        DATABASE_KEY (str): The key to fetch the database name.
        SCHEMA_KEY (str): The key to fetch the schema name.

    Usage:
        Subclass this interface and implement the required attributes and any methods
        that need custom behavior (ex. fetch_metadata).

        >>> class MySQLWorkflowMetadataInterface(SQLWorkflowMetadataInterface):
        >>>     METADATA_SQL = "SELECT * FROM information_schema.schemata"
        >>>     DATABASE_KEY = "TABLE_CATALOG"
        >>>     SCHEMA_KEY = "TABLE_SCHEMA"
        >>>     def __init__(self, create_engine_fn: Callable[[Dict[str, Any]], Engine]):
        >>>         super().__init__(create_engine_fn)
    """
    semaphore_concurrency: int = int(os.getenv("SEMAPHORE_CONCURRENCY", 5))

    # Create a context variable to hold the semaphore
    semaphore_context = contextvars.ContextVar("semaphore")

    async def get_full_databases(self):
        get_databases_query = """
            SHOW DATABASES;
        """
        get_databases_input = self.sql_resource.sql_input(
            engine=self.sql_resource.engine,
            query=get_databases_query,
        )

        get_databases_result = await get_databases_input.get_batched_dataframe()

        full_database_list = []

        for batch in get_databases_result:
            if isinstance(batch, pd.DataFrame):
                full_database_list.append(batch)
            else:
                for df in batch:
                    full_database_list.append(df)

        return pd.concat(full_database_list, ignore_index=True)

    async def fetch_db_metadata(self, database_name: str, semaphore: asyncio.Semaphore):
        async with semaphore:
            query = self.METADATA_SQL.format(database_name=database_name)
            if not self.sql_resource:
                raise ValueError("SQL Resource not defined")

            args = {
                "metadata_sql": query,
                "database_alias_key": self.DATABASE_ALIAS_KEY,
                "schema_alias_key": self.SCHEMA_ALIAS_KEY,
                "database_result_key": self.DATABASE_KEY,
                "schema_result_key": self.SCHEMA_KEY,
            }
            return await self.sql_resource.fetch_metadata(args)

    async def fetch_metadata(self) -> List[Dict[str, str]]:
        databases = await self.get_full_databases()
        full_database_list = databases["name"].tolist()

        # Create a semaphore if not present in the context
        semaphore = self.semaphore_context.get(
            asyncio.Semaphore(self.semaphore_concurrency)
        )

        fetch_databases_metadata = []
        for db_name in full_database_list:
            fetch_databases_metadata.append(
                self.fetch_db_metadata(
                    db_name,
                    semaphore,
                )
            )

        # Use asyncio.gather to execute all tasks concurrently and collect the results
        results = await asyncio.gather(*fetch_databases_metadata)

        # Flatten the list of lists into a single list and filter out INFORMATION_SCHEMA entries
        flattened_results = list(itertools.chain(*results))

        return flattened_results
