import asyncio
import concurrent
import logging
from typing import Any, Dict, Iterator, Optional, Union

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.session import Session

from application_sdk.activities import ActivitiesState
from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.common.utils import prepare_query
from application_sdk.inputs import Input

logger = AtlanLoggerAdapter(logging.getLogger(__name__))


def _get_sql_query(
    query_attribute: str, workflow_args: Dict[str, Any], parent_class: Optional[Any]
) -> str:
    """Get the SQL query to execute.

    Returns:
        str: The SQL query to execute.
    """
    # Check if the parent class has the query defined and process the same
    if parent_class and hasattr(parent_class, query_attribute):
        return prepare_query(getattr(parent_class, query_attribute), workflow_args)

    # Check if the workflow_args have the query defined and process the same
    # This is applicable in case of query miner workflow
    if workflow_args.get(query_attribute):
        return workflow_args.get(query_attribute)

    return query_attribute


class SQLQueryInput(Input):
    """Input handler for SQL queries.

    This class provides asynchronous functionality to execute SQL queries and return
    results as DataFrames, with support for both pandas and daft formats.

    Attributes:
        query (str): The SQL query to execute.
        engine (Union[Engine, str]): SQLAlchemy engine or connection string.
        chunk_size (Optional[int]): Number of rows to fetch per batch.
        state (Optional[ActivitiesState]): State object for the activity.
        async_session: Async session maker for database operations.
    """

    query: str
    engine: Optional[Union[Engine, str]]
    chunk_size: Optional[int]
    state: Optional[ActivitiesState] = None
    async_session: Optional[AsyncSession] = None

    def __init__(
        self,
        query: str,
        engine: Optional[Union[Engine, str]] = None,
        chunk_size: Optional[int] = 100000,
        **kwargs: Dict[str, Any],
    ):
        """Initialize the async SQL query input handler.

        Args:
            engine (Union[Engine, str]): SQLAlchemy engine or connection string.
            query (str): The SQL query to execute.
            chunk_size (Optional[int], optional): Number of rows per batch.
                Defaults to 100000.
        """
        self.query = query
        self.engine = engine
        self.chunk_size = chunk_size
        if self.engine and isinstance(self.engine, AsyncEngine):
            self.async_session = sessionmaker(
                self.engine, expire_on_commit=False, class_=AsyncSession
            )

    @classmethod
    def re_init(
        cls,
        query: str,
        state: ActivitiesState,
        parent_class: Any,
        **kwargs: Dict[str, Any],
    ):
        """Re-initialize the input class with given keyword arguments.

        Args:
            query (str): The SQL query attribute to fetch the query from parent class.
            state (ActivitiesState): State object for the activity.
            parent_class (Any): Parent class object.
            **kwargs (Dict[str, Any]): Keyword arguments for re-initialization.
        """
        engine = kwargs.get("engine")
        if not engine:
            engine = (
                state.sql_client.engine if state else parent_class.sql_client.engine
            )

        kwargs["engine"] = engine
        kwargs["query"] = _get_sql_query(query, kwargs, parent_class)
        return cls(**kwargs)

    def _read_sql_query(
        self, session: Session
    ) -> Union[pd.DataFrame, Iterator[pd.DataFrame]]:
        """Execute SQL query using the provided session.

        Args:
            session: SQLAlchemy session for database operations.

        Returns:
            Union[pd.DataFrame, Iterator[pd.DataFrame]]: Query results as DataFrame
                or iterator of DataFrames if chunked.
        """
        conn = session.connection()
        return pd.read_sql_query(text(self.query), conn, chunksize=self.chunk_size)

    def _execute_query(self) -> Union[pd.DataFrame, Iterator[pd.DataFrame]]:
        """Execute SQL query using the provided engine and pandas.

        Returns:
            Union[pd.DataFrame, Iterator[pd.DataFrame]]: Query results as DataFrame
                or iterator of DataFrames if chunked.
        """
        with self.engine.connect() as conn:
            return pd.read_sql_query(text(self.query), conn, chunksize=self.chunk_size)

    async def get_batched_dataframe(self) -> Iterator[pd.DataFrame]:
        """Get query results as batched pandas DataFrames asynchronously.

        Returns:
            Iterator[pd.DataFrame]: Iterator yielding batches of query results.

        Raises:
            ValueError: If engine is a string instead of SQLAlchemy engine.
            Exception: If there's an error executing the query.
        """
        try:
            if isinstance(self.engine, str):
                raise ValueError("Engine should be an SQLAlchemy engine object")

            if self.async_session:
                async with self.async_session() as session:
                    return await session.run_sync(self._read_sql_query)
            else:
                # Run the blocking operation in a thread pool
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    return await asyncio.get_event_loop().run_in_executor(
                        executor, self._execute_query
                    )
        except Exception as e:
            logger.error(f"Error reading batched data(pandas) from SQL: {str(e)}")

    async def get_dataframe(self) -> pd.DataFrame:
        """Get all query results as a single pandas DataFrame asynchronously.

        Returns:
            pd.DataFrame: Query results as a DataFrame.

        Raises:
            ValueError: If engine is a string instead of SQLAlchemy engine.
            Exception: If there's an error executing the query.
        """
        try:
            if isinstance(self.engine, str):
                raise ValueError("Engine should be an SQLAlchemy engine object")

            if self.async_session:
                async with self.async_session() as session:
                    return await session.run_sync(self._read_sql_query)
            else:
                # Run the blocking operation in a thread pool
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    return await asyncio.get_event_loop().run_in_executor(
                        executor, self._execute_query
                    )
        except Exception as e:
            logger.error(f"Error reading data(pandas) from SQL: {str(e)}")

    async def get_daft_dataframe(self) -> "daft.DataFrame":  # noqa: F821
        """Get query results as a daft DataFrame.

        This method uses ConnectorX to read data from SQL for supported connectors.
        For unsupported connectors and direct engine usage, it falls back to SQLAlchemy.

        Returns:
            daft.DataFrame: Query results as a daft DataFrame.

        Raises:
            ValueError: If engine is a string instead of SQLAlchemy engine.
            Exception: If there's an error executing the query.

        Note:
            For ConnectorX supported sources, see:
            https://sfu-db.github.io/connector-x/intro.html#sources
        """
        try:
            # Daft uses ConnectorX to read data from SQL by default for supported connectors
            # If a connection string is passed, it will use ConnectorX to read data
            # For unsupported connectors and if directly engine is passed, it will use SQLAlchemy
            import daft

            if isinstance(self.engine, str):
                return daft.read_sql(self.query, self.engine)
            return daft.read_sql(self.query, lambda: self.engine.connect())
        except Exception as e:
            logger.error(f"Error reading data(daft) from SQL: {str(e)}")

    async def get_batched_daft_dataframe(self) -> Iterator["daft.DataFrame"]:  # noqa: F821
        """Get query results as batched daft DataFrames.

        This method reads data using pandas in batches since daft does not support
        batch reading. Each pandas DataFrame is then converted to a daft DataFrame.

        Returns:
            Iterator[daft.DataFrame]: Iterator yielding batches of query results
                as daft DataFrames.

        Raises:
            ValueError: If engine is a string instead of SQLAlchemy engine.
            Exception: If there's an error executing the query.

        Note:
            This method uses pandas for batch reading since daft does not support
            reading data in batches natively.
        """
        try:
            import daft

            if isinstance(self.engine, str):
                raise ValueError("Engine should be an SQLAlchemy engine object")

            batched_df = await self.get_batched_dataframe()
            for df in batched_df:
                daft_df = daft.from_pandas(df)
                yield daft_df
        except Exception as e:
            logger.error(f"Error reading batched data(daft) from SQL: {str(e)}")
