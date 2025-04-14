import asyncio
import concurrent
from typing import (
    TYPE_CHECKING,
    AsyncGenerator,
    AsyncIterator,
    Iterator,
    Optional,
    Union,
)

from application_sdk.common.logger_adaptors import get_logger
from application_sdk.inputs import Input

logger = get_logger(__name__)

if TYPE_CHECKING:
    import daft
    import pandas as pd
    from sqlalchemy.engine import Engine
    from sqlalchemy.orm import Session


class SQLQueryInput(Input):
    """Input handler for SQL queries.

    This class provides asynchronous functionality to execute SQL queries and return
    results as DataFrames, with support for both pandas and daft formats.

    Attributes:
        query (str): The SQL query to execute.
        engine (Union[Engine, str]): SQLAlchemy engine or connection string.
        chunk_size (Optional[int]): Number of rows to fetch per batch.
    """

    query: str
    engine: Union["Engine", str]
    chunk_size: Optional[int]

    def __init__(
        self,
        query: str,
        engine: Union["Engine", str],
        chunk_size: Optional[int] = 100000,
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

    def _read_sql_query(
        self, session: "Session"
    ) -> Union["pd.DataFrame", Iterator["pd.DataFrame"]]:
        """Execute SQL query using the provided session.

        Args:
            session: SQLAlchemy session for database operations.

        Returns:
            Union["pd.DataFrame", Iterator["pd.DataFrame"]]: Query results as DataFrame
                or iterator of DataFrames if chunked.
        """
        import pandas as pd
        from sqlalchemy import text

        conn = session.connection()
        return pd.read_sql_query(text(self.query), conn, chunksize=self.chunk_size)

    def _execute_query_daft(
        self,
    ) -> Union["daft.DataFrame", Iterator["daft.DataFrame"]]:
        """Execute SQL query using the provided engine and daft.

        Returns:
            Union["daft.DataFrame", Iterator["daft.DataFrame"]]: Query results as DataFrame
                or iterator of DataFrames if chunked.
        """
        # Daft uses ConnectorX to read data from SQL by default for supported connectors
        # If a connection string is passed, it will use ConnectorX to read data
        # For unsupported connectors and if directly engine is passed, it will use SQLAlchemy
        import daft

        if isinstance(self.engine, str):
            return daft.read_sql(self.query, self.engine)
        return daft.read_sql(self.query, self.engine.connect)

    def _execute_query(self) -> Union["pd.DataFrame", Iterator["pd.DataFrame"]]:
        """Execute SQL query using the provided engine and pandas.

        Returns:
            Union["pd.DataFrame", Iterator["pd.DataFrame"]]: Query results as DataFrame
                or iterator of DataFrames if chunked.
        """
        if not self.engine:
            raise ValueError("Engine is not defined")

        with self.engine.connect() as conn:
            import pandas as pd
            from sqlalchemy import text

            return pd.read_sql_query(text(self.query), conn, chunksize=self.chunk_size)

    def get_batched_dataframe(self) -> Iterator["pd.DataFrame"]:
        """Get query results as batched pandas DataFrames.

        Returns:
            Iterator["pd.DataFrame"]: Iterator yielding batches of query results.
        """
        import asyncio

        # Define a wrapper coroutine that collects results from the async method
        async def _wrapper():
            results = []
            try:
                async for batch in self._get_batched_dataframe_async():
                    results.append(batch)
            except Exception as e:
                logger.error(f"Error in get_batched_dataframe: {str(e)}")
                raise e
            return results

        # Run the coroutine and return an iterator of the results
        return iter(asyncio.run(_wrapper()))

    async def _get_batched_dataframe_async(
        self,
    ) -> AsyncGenerator["pd.DataFrame", None]:
        """Async implementation for batched dataframe retrieval

        Returns:
            AsyncGenerator["pd.DataFrame", None]: Async generator yielding batches
        """
        try:
            import pandas as pd

            if isinstance(self.engine, str):
                raise ValueError("Engine should be an SQLAlchemy engine object")

            from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

            async_session = None
            if self.engine and isinstance(self.engine, AsyncEngine):
                from sqlalchemy.orm import sessionmaker

                async_session = sessionmaker(
                    self.engine, expire_on_commit=False, class_=AsyncSession
                )

            if async_session:
                async with async_session() as session:
                    result = await session.run_sync(self._read_sql_query)
                    if isinstance(result, pd.DataFrame):
                        yield result
                    elif isinstance(result, Iterator):
                        for batch in result:
                            yield batch
            else:
                # Run the blocking operation in a thread pool
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    result = await asyncio.get_event_loop().run_in_executor(
                        executor, self._execute_query
                    )
                    if isinstance(result, pd.DataFrame):
                        yield result
                    elif isinstance(result, Iterator):
                        for batch in result:
                            yield batch
        except Exception as e:
            logger.error(f"Error reading batched data(pandas) from SQL: {str(e)}")
            raise e

    def get_dataframe(self) -> "pd.DataFrame":
        """Get all query results as a single pandas DataFrame.

        Returns:
            pd.DataFrame: Query results as a DataFrame.
        """
        import asyncio

        return asyncio.run(self._get_dataframe_async())

    async def _get_dataframe_async(self) -> "pd.DataFrame":
        """Async implementation of get_dataframe"""
        try:
            import pandas as pd

            if isinstance(self.engine, str):
                raise ValueError("Engine should be an SQLAlchemy engine object")

            from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

            async_session = None
            if self.engine and isinstance(self.engine, AsyncEngine):
                from sqlalchemy.orm import sessionmaker

                async_session = sessionmaker(
                    self.engine, expire_on_commit=False, class_=AsyncSession
                )

            if async_session:
                async with async_session() as session:
                    result = await session.run_sync(self._read_sql_query)
                    if isinstance(result, pd.DataFrame):
                        return result
                    elif isinstance(result, Iterator):
                        # Combine all batches into a single DataFrame
                        return pd.concat(list(result), ignore_index=True)
                    return pd.DataFrame()
            else:
                # Run the blocking operation in a thread pool
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    result = await asyncio.get_event_loop().run_in_executor(
                        executor, self._execute_query
                    )
                    if isinstance(result, pd.DataFrame):
                        return result
                    elif isinstance(result, Iterator):
                        # Combine all batches into a single DataFrame
                        return pd.concat(list(result), ignore_index=True)
                    return pd.DataFrame()
        except Exception as e:
            logger.error(f"Error reading data(pandas) from SQL: {str(e)}")
            raise e

    async def _get_daft_dataframe_async(self) -> "daft.DataFrame":
        """Async implementation of get_daft_dataframe"""
        try:
            import daft

            # Run the blocking operation in a thread pool
            with concurrent.futures.ThreadPoolExecutor() as executor:
                result = await asyncio.get_event_loop().run_in_executor(
                    executor, self._execute_query_daft
                )
                if result is None:
                    return daft.DataFrame()
                return result
        except Exception as e:
            logger.error(f"Error reading data(daft) from SQL: {str(e)}")
            raise e

    def get_batched_daft_dataframe(
        self,
    ) -> Union[Iterator["daft.DataFrame"], AsyncIterator["daft.DataFrame"]]:
        """Get query results as batched daft DataFrames.

        Returns:
            Union[Iterator["daft.DataFrame"], AsyncIterator["daft.DataFrame"]]: Iterator of daft DataFrames
        """
        import asyncio

        # Define a wrapper coroutine that collects results from the async method
        async def _wrapper():
            results = []
            try:
                async for batch in self._get_batched_daft_dataframe_async():
                    results.append(batch)
            except Exception as e:
                logger.error(f"Error in get_batched_daft_dataframe: {str(e)}")
                raise e
            return results

        # Run the coroutine and return an iterator of the results
        return iter(asyncio.run(_wrapper()))

    async def _get_batched_daft_dataframe_async(
        self,
    ) -> AsyncGenerator["daft.DataFrame", None]:
        """Async implementation for batched daft dataframe retrieval

        Returns:
            AsyncGenerator["daft.DataFrame", None]: Async generator yielding daft batches
        """
        try:
            import daft

            if isinstance(self.engine, str):
                raise ValueError("Engine should be an SQLAlchemy engine object")

            async for dataframe in self._get_batched_dataframe_async():
                if dataframe is not None:
                    daft_dataframe = daft.from_pandas(dataframe)
                    yield daft_dataframe
        except Exception as e:
            logger.error(f"Error reading batched data(daft) from SQL: {str(e)}")
            raise e

    async def get_daft_dataframe(self) -> "daft.DataFrame":
        """Get query results as a daft DataFrame.

        This method can be awaited directly or run synchronously depending on context.

        Returns:
            daft.DataFrame: Query results as a daft DataFrame.
        """
        return await self._get_daft_dataframe_async()
