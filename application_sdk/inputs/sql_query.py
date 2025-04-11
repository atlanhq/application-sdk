import asyncio
import concurrent
from typing import TYPE_CHECKING, Any, AsyncIterator, Dict, Iterator, Optional, Union

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.session import Session

from application_sdk.activities import ActivitiesState
from application_sdk.common.logger_adaptors import get_logger
from application_sdk.common.utils import prepare_query
from application_sdk.inputs import Input

logger = get_logger(__name__)

if TYPE_CHECKING:
    import daft


def _get_sql_query(
    query_attribute: str,
    workflow_args: Dict[str, Any],
    parent_class: Optional[Any],
    temp_table_sql_query: Optional[str] = None,
) -> Union[str, None]:
    """Get the SQL query to execute.

    Returns:
        Union[str, None]: The SQL query to execute.
    """
    # Check if the parent class has the query defined and process the same
    if parent_class and hasattr(parent_class, query_attribute):
        query_value = getattr(parent_class, query_attribute)
        if temp_table_sql_query and hasattr(parent_class, temp_table_sql_query):
            temp_value = getattr(parent_class, temp_table_sql_query)
            result = prepare_query(query_value, workflow_args, temp_value)
            return result if isinstance(result, str) else None
        else:
            result = prepare_query(query_value, workflow_args)
            return result if isinstance(result, str) else None

    # Check if the workflow_args have the query defined and process the same
    # This is applicable in case of query miner workflow
    if (
        workflow_args
        and isinstance(workflow_args, dict)
        and workflow_args.get(query_attribute)
    ):
        query = workflow_args.get(query_attribute)
        if isinstance(query, str):
            return query

    # Return the query attribute itself if it's a string, otherwise return None
    return query_attribute if isinstance(query_attribute, str) else None


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
    temp_table_sql_query: Optional[str] = None

    def __init__(
        self,
        query: str,
        engine: Optional[Union[Engine, str]] = None,
        chunk_size: Optional[int] = 100000,
        temp_table_sql_query: Optional[str] = None,
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
        self.temp_table_sql_query = temp_table_sql_query
        if self.engine and isinstance(self.engine, AsyncEngine):
            self.async_session = sessionmaker(
                self.engine, expire_on_commit=False, class_=AsyncSession
            )

    @classmethod
    def re_init(cls, **kwargs: Dict[str, Any]):
        """Re-initialize the input class with given keyword arguments.

        Args:
            **kwargs (Dict[str, Any]): Keyword arguments for re-initialization.

        Returns:
            SQLQueryInput: An instance of the SQLQueryInput class.
        """
        # Extract the key parameters from kwargs
        query = str(kwargs.get("query", ""))
        state = kwargs.get("state")
        parent_class = kwargs.get("parent_class")
        temp_table_sql_query = kwargs.get("temp_table_sql_query")

        if isinstance(temp_table_sql_query, str) or temp_table_sql_query is None:
            processed_temp_table_sql_query = temp_table_sql_query
        else:
            # Convert to string or set to None if invalid
            processed_temp_table_sql_query = (
                str(temp_table_sql_query) if temp_table_sql_query else None
            )

        # Extract the engine from state or parent_class
        engine = kwargs.get("engine")
        if not engine:
            if (
                state
                and hasattr(state, "sql_client")
                and state.sql_client
                and hasattr(state.sql_client, "engine")
            ):
                engine = state.sql_client.engine
            elif (
                parent_class
                and hasattr(parent_class, "sql_client")
                and parent_class.sql_client
                and hasattr(parent_class.sql_client, "engine")
            ):
                engine = parent_class.sql_client.engine

        # Get the processed SQL query
        processed_query = _get_sql_query(
            query, kwargs, parent_class, processed_temp_table_sql_query
        )
        if processed_query is None:
            raise ValueError("Query is not defined")

        # Create a new instance with cleaned parameters
        chunk_size = kwargs.get("chunk_size")
        if not isinstance(chunk_size, (int, type(None))):
            chunk_size = 100000  # Default if invalid type

        return cls(
            query=processed_query,
            engine=engine,
            chunk_size=chunk_size,
            temp_table_sql_query=processed_temp_table_sql_query,
        )

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
        if not self.engine:
            raise ValueError("Engine is not defined")

        with self.engine.connect() as conn:
            return pd.read_sql_query(text(self.query), conn, chunksize=self.chunk_size)

    async def get_batched_dataframe(self) -> AsyncIterator[pd.DataFrame]:
        """Get query results as batched pandas DataFrames asynchronously.

        Returns:
            AsyncIterator[pd.DataFrame]: Iterator yielding batches of query results.

        Raises:
            ValueError: If engine is a string instead of SQLAlchemy engine.
            Exception: If there's an error executing the query.
        """
        try:
            if isinstance(self.engine, str):
                raise ValueError("Engine should be an SQLAlchemy engine object")

            if self.async_session:
                async with self.async_session() as session:
                    result = await session.run_sync(self._read_sql_query)
                    if isinstance(result, pd.DataFrame):
                        yield result
                    else:
                        for df in result:
                            yield df
                    return
            else:
                # Run the blocking operation in a thread pool
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    result = await asyncio.get_event_loop().run_in_executor(
                        executor, self._execute_query
                    )
                    if isinstance(result, pd.DataFrame):
                        yield result
                    else:
                        for df in result:
                            yield df
                    return
        except Exception as e:
            logger.error(f"Error reading batched data(pandas) from SQL: {str(e)}")
            raise e

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
                    result = await session.run_sync(self._read_sql_query)
                    if isinstance(result, pd.DataFrame):
                        return result
                    else:
                        # Combine all batches into a single DataFrame
                        all_data = []
                        for df in result:
                            all_data.append(df)
                        if all_data:
                            return pd.concat(all_data)
                        return pd.DataFrame()
            else:
                # Run the blocking operation in a thread pool
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    result = await asyncio.get_event_loop().run_in_executor(
                        executor, self._execute_query
                    )
                    if isinstance(result, pd.DataFrame):
                        return result
                    else:
                        # Combine all batches into a single DataFrame
                        all_data = []
                        for df in result:
                            all_data.append(df)
                        if all_data:
                            return pd.concat(all_data)
                        return pd.DataFrame()
        except Exception as e:
            logger.error(f"Error reading data(pandas) from SQL: {str(e)}")
            raise e

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
            # Import daft locally
            import daft

            # Create an empty DataFrame as fallback

            if not self.engine:
                raise ValueError("Engine is not defined")

            if isinstance(self.engine, str):
                return daft.read_sql(self.query, self.engine)

            # Use a safe connect function that checks for None
            def safe_connect():
                if self.engine is None:
                    raise ValueError("Engine is None")
                return self.engine.connect()

            # Use the safe connect function in the lambda
            return daft.read_sql(self.query, lambda: safe_connect())
        except Exception as e:
            logger.error(f"Error reading data(daft) from SQL: {str(e)}")
            # When an error occurs, create and return an empty daft DataFrame
            try:
                import daft

                return daft.DataFrame()
            except Exception:
                # Re-raise the original error if we can't create an empty DataFrame
                raise e

    async def get_batched_daft_dataframe(self) -> AsyncIterator["daft.DataFrame"]:  # noqa: F821
        """Get query results as batched daft DataFrames.

        This method reads data using pandas in batches since daft does not support
        batch reading. Each pandas DataFrame is then converted to a daft DataFrame.

        Returns:
            AsyncIterator[daft.DataFrame]: Iterator yielding batches of query results
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

            async for dataframe in self.get_batched_dataframe():
                daft_dataframe = daft.from_pandas(dataframe)
                yield daft_dataframe
        except Exception as e:
            logger.error(f"Error reading batched data(daft) from SQL: {str(e)}")
            raise e
