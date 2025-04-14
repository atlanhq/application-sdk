import asyncio
import concurrent
from typing import TYPE_CHECKING, Any, AsyncIterator, Iterator, Optional, Union

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
        self.engine = engine

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
        with self.engine.connect() as conn:
            import pandas as pd
            from sqlalchemy import text

            return pd.read_sql_query(text(self.query), conn, chunksize=self.chunk_size)

    def get_batched_dataframe(
        self,
    ) -> Union[Iterator["pd.DataFrame"], AsyncIterator["pd.DataFrame"]]:
        """
        Get query results as batched pandas DataFrames.

        This method wraps the async implementation for synchronous use.

        Returns:
            Iterator["pd.DataFrame"]: Iterator yielding batches of query results.

        Raises:
            ValueError: If engine is a string instead of SQLAlchemy engine.
            Exception: If there's an error executing the query.
        """
        import asyncio

        async_gen = self._get_batched_dataframe_async()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            results = []
            while True:
                try:
                    batch = loop.run_until_complete(async_gen.__anext__())
                    results.append(batch)
                except StopAsyncIteration:
                    break
            return iter(results)
        finally:
            loop.close()

    async def _get_batched_dataframe_async(self) -> AsyncIterator["pd.DataFrame"]:
        """
        Async implementation of get_batched_dataframe
        """
        try:
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
                # Type checking workaround for AsyncSession
                session_obj: Any = None
                async with async_session() as session:  # type: ignore
                    session_obj = session
                    result = await session_obj.run_sync(self._read_sql_query)
                    for chunk in result:
                        yield chunk
            else:
                # Run the blocking operation in a thread pool
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    result = await asyncio.get_event_loop().run_in_executor(
                        executor, self._execute_query
                    )
                    for chunk in result:
                        yield chunk
        except Exception as e:
            logger.error(f"Error reading batched data(pandas) from SQL: {str(e)}")
            raise e

    def get_dataframe(self) -> "pd.DataFrame":
        """
        Get all query results as a single pandas DataFrame.

        This method wraps the async implementation for synchronous use.

        Returns:
            pd.DataFrame: Query results as a DataFrame.

        Raises:
            ValueError: If engine is a string instead of SQLAlchemy engine.
            Exception: If there's an error executing the query.
        """
        import asyncio

        async_func = self._get_dataframe_async()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(async_func)
        finally:
            loop.close()

    async def _get_dataframe_async(self) -> "pd.DataFrame":
        """
        Async implementation of get_dataframe
        """
        try:
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
                # Type checking workaround for AsyncSession
                session_obj: Any = None
                async with async_session() as session:  # type: ignore
                    session_obj = session
                    df = await session_obj.run_sync(self._read_sql_query)
                    # Ensure we're returning a DataFrame, not an iterator
                    import pandas as pd

                    if isinstance(df, Iterator):
                        result = pd.concat(list(df), ignore_index=True)
                        return result
                    return df
            else:
                # Run the blocking operation in a thread pool
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    df = await asyncio.get_event_loop().run_in_executor(
                        executor, self._execute_query
                    )
                    # Ensure we're returning a DataFrame, not an iterator
                    import pandas as pd

                    if isinstance(df, Iterator):
                        result = pd.concat(list(df), ignore_index=True)
                        return result
                    return df
        except Exception as e:
            logger.error(f"Error reading data(pandas) from SQL: {str(e)}")
            raise e

    def get_daft_dataframe(self) -> "daft.DataFrame":
        """
        Get query results as a daft DataFrame.

        This method wraps the async implementation for synchronous use.

        Returns:
            daft.DataFrame: Query results as a daft DataFrame.
        """
        import asyncio

        async_func = self._get_daft_dataframe_async()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(async_func)
        finally:
            loop.close()

    async def _get_daft_dataframe_async(self) -> "daft.DataFrame":
        """
        Async implementation of get_daft_dataframe
        """
        try:
            # Run the blocking operation in a thread pool
            with concurrent.futures.ThreadPoolExecutor() as executor:
                result = await asyncio.get_event_loop().run_in_executor(
                    executor, self._execute_query_daft
                )
                # If result is an iterator, take just the first item
                import daft

                if isinstance(result, Iterator):
                    # Take the first DataFrame from the iterator
                    for df in result:
                        return df
                    # If iterator is empty, return empty DataFrame
                    return daft.DataFrame()
                return result
        except Exception as e:
            logger.error(f"Error reading data(daft) from SQL: {str(e)}")
            raise e

    def get_batched_daft_dataframe(self) -> Iterator["daft.DataFrame"]:
        """
        Get query results as batched daft DataFrames.

        This method wraps the async implementation for synchronous use.

        Returns:
            Iterator[daft.DataFrame]: Iterator yielding batches of query results
                as daft DataFrames.
        """
        import asyncio

        def run_async():
            async_gen = self._get_batched_daft_dataframe_async()
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                results = []
                while True:
                    try:
                        future = asyncio.ensure_future(async_gen.__anext__())
                        batch = loop.run_until_complete(future)
                        results.append(batch)
                    except StopAsyncIteration:
                        break
                return results
            finally:
                loop.close()

        return iter(run_async())

    async def _get_batched_daft_dataframe_async(
        self,
    ) -> AsyncIterator["daft.DataFrame"]:
        """
        Async implementation of get_batched_daft_dataframe
        """
        try:
            import daft

            if isinstance(self.engine, str):
                raise ValueError("Engine should be an SQLAlchemy engine object")

            # Don't await the iterator directly, iterate through it with async for
            gen = self._get_batched_dataframe_async()
            async for dataframe in gen:
                daft_dataframe = daft.from_pandas(dataframe)
                yield daft_dataframe
        except Exception as e:
            logger.error(f"Error reading batched data(daft) from SQL: {str(e)}")
            raise e
