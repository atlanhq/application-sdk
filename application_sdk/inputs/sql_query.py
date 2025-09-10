import asyncio
import concurrent
from typing import TYPE_CHECKING, AsyncIterator, Iterator, Optional, Union

from application_sdk.inputs import Input
from application_sdk.observability.logger_adaptor import get_logger

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

    def _execute_pandas_query(
        self, conn
    ) -> Union["pd.DataFrame", Iterator["pd.DataFrame"]]:
        """Helper function to execute SQL query using pandas.
           The function is responsible for using import_optional_dependency method of the pandas library to import sqlalchemy
           This function helps pandas in determining weather to use the sqlalchemy connection object and constructs like text()
           or use the underlying database connection object. This has been done to make sure connectors like the Redshift connector,
           which do not support the sqlalchemy connection object, can be made compatible with the application-sdk.

        Args:
            conn: Database connection object.

        Returns:
            Union["pd.DataFrame", Iterator["pd.DataFrame"]]: Query results as DataFrame
                or iterator of DataFrames if chunked.
        """
        import pandas as pd
        from pandas.compat._optional import import_optional_dependency
        from sqlalchemy import text

        if import_optional_dependency("sqlalchemy", errors="ignore"):
            return pd.read_sql_query(text(self.query), conn, chunksize=self.chunk_size)
        else:
            dbapi_conn = getattr(conn, "connection", None)
            return pd.read_sql_query(self.query, dbapi_conn, chunksize=self.chunk_size)

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
        conn = session.connection()
        return self._execute_pandas_query(conn)

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
            return daft.read_sql(
                self.query, self.engine, infer_schema_length=self.chunk_size
            )
        return daft.read_sql(
            self.query, self.engine.connect, infer_schema_length=self.chunk_size
        )

    def _execute_query(self) -> Union["pd.DataFrame", Iterator["pd.DataFrame"]]:
        """Execute SQL query using the provided engine and pandas.

        Returns:
            Union["pd.DataFrame", Iterator["pd.DataFrame"]]: Query results as DataFrame
                or iterator of DataFrames if chunked.
        """
        with self.engine.connect() as conn:
            return self._execute_pandas_query(conn)

    async def get_batched_dataframe(
        self,
    ) -> Union[AsyncIterator["pd.DataFrame"], Iterator["pd.DataFrame"]]:  # type: ignore
        """Get query results as batched pandas DataFrames asynchronously.

        Returns:
            AsyncIterator["pd.DataFrame"]: Async iterator yielding batches of query results.

        Raises:
            ValueError: If engine is a string instead of SQLAlchemy engine.
            Exception: If there's an error executing the query.
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
                async with async_session() as session:
                    return await session.run_sync(self._read_sql_query)
            else:
                # Run the blocking operation in a thread pool
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    return await asyncio.get_event_loop().run_in_executor(  # type: ignore
                        executor, self._execute_query
                    )
        except Exception as e:
            logger.error(f"Error reading batched data(pandas) from SQL: {str(e)}")

    async def get_dataframe(self) -> "pd.DataFrame":
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

            from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

            async_session = None
            if self.engine and isinstance(self.engine, AsyncEngine):
                from sqlalchemy.orm import sessionmaker

                async_session = sessionmaker(
                    self.engine, expire_on_commit=False, class_=AsyncSession
                )

            if async_session:
                async with async_session() as session:
                    return await session.run_sync(self._read_sql_query)
            else:
                # Run the blocking operation in a thread pool
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    result = await asyncio.get_event_loop().run_in_executor(
                        executor, self._execute_query
                    )
                    import pandas as pd

                    if isinstance(result, pd.DataFrame):
                        return result
                    raise Exception(
                        "Unable to get pandas dataframe from SQL query results"
                    )

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
            import daft

            # Run the blocking operation in a thread pool
            with concurrent.futures.ThreadPoolExecutor() as executor:
                result = await asyncio.get_event_loop().run_in_executor(
                    executor, self._execute_query_daft
                )
                if isinstance(result, daft.DataFrame):
                    return result
                raise
        except Exception as e:
            logger.error(f"Error reading data(daft) from SQL: {str(e)}")
            raise

    async def get_batched_daft_dataframe(
        self,
    ) -> Union[AsyncIterator["daft.DataFrame"], Iterator["daft.DataFrame"]]:  # noqa: F821
        """Get query results as batched daft DataFrames.

        This method reads data using pandas in batches since daft does not support
        batch reading. Each pandas DataFrame is then converted to a daft DataFrame.

        Returns:
            AsyncIterator[daft.DataFrame]: Async iterator yielding batches of query results
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

            # Use async for to consume the AsyncIterator properly
            async for dataframe in self.get_batched_dataframe():
                daft_dataframe = daft.from_pandas(dataframe)
                yield daft_dataframe
        except Exception as e:
            logger.error(f"Error reading batched data(daft) from SQL: {str(e)}")
