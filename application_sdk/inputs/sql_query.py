import asyncio
import concurrent
from typing import Iterator, Optional, Union

import daft
import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from application_sdk import logging
from application_sdk.inputs import Input

logger = logging.get_logger(__name__)


class SQLQueryInput(Input):
    query: str
    engine: Union[Engine, str]
    chunk_size: Optional[int]

    def __init__(
        self, engine: Union[Engine, str], query: str, chunk_size: Optional[int] = 100000
    ):
        self.query = query
        self.engine = engine
        self.chunk_size = chunk_size

    async def get_batched_dataframe(self) -> Iterator[pd.DataFrame]:
        try:
            if isinstance(self.engine, str):
                raise ValueError("Engine should be an SQLAlchemy engine object")

            def _execute_query():
                with self.engine.connect() as conn:
                    return pd.read_sql_query(
                        text(self.query), conn, chunksize=self.chunk_size
                    )

            # Run the blocking operation in a thread pool
            with concurrent.futures.ThreadPoolExecutor() as executor:
                return await asyncio.get_event_loop().run_in_executor(
                    executor, _execute_query
                )
        except Exception as e:
            logger.error(f"Error reading batched data(pandas) from SQL: {str(e)}")

    async def get_dataframe(self) -> pd.DataFrame:
        try:
            if isinstance(self.engine, str):
                raise ValueError("Engine should be an SQLAlchemy engine object")

            def _execute_query():
                with self.engine.connect() as conn:
                    return pd.read_sql_query(text(self.query), conn)

            # Run the blocking operation in a thread pool
            with concurrent.futures.ThreadPoolExecutor() as executor:
                return await asyncio.get_event_loop().run_in_executor(
                    executor, _execute_query
                )
        except Exception as e:
            logger.error(f"Error reading data(pandas) from SQL: {str(e)}")

    async def get_daft_dataframe(self) -> daft.DataFrame:
        """
        Method to read data from SQL using daft and return as daft dataframe
        """
        try:
            # Daft uses ConnectorX to read data from SQL by default for supported connectors
            # If a connection string is passed, it will use ConnectorX to read data
            # For unsupported connectors and if directly engine is passed, it will use SQLAlchemy
            if isinstance(self.engine, str):
                return daft.read_sql(self.query, self.engine)
            return daft.read_sql(self.query, lambda: self.engine.connect())
        except Exception as e:
            logger.error(f"Error reading data(daft) from SQL: {str(e)}")

    async def get_batched_daft_dataframe(self) -> daft.DataFrame:
        """
        Method to read data from SQL using pandas in batches and return as daft dataframe
        We get the data using pandas since daft does not support reading data in batches
        This pandas data will then be converted to daft dataframe
        """
        try:
            if isinstance(self.engine, str):
                raise ValueError("Engine should be an SQLAlchemy engine object")

            batched_df = await self.get_batched_dataframe()
            for df in batched_df:
                daft_df = daft.from_pandas(df)
                yield daft_df
        except Exception as e:
            logger.error(f"Error reading batched data(daft) from SQL: {str(e)}")


class AsyncSQLQueryInput(Input):
    query: str
    engine: Union[Engine, str]
    chunk_size: Optional[int]

    def __init__(
        self, engine: Union[Engine, str], query: str, chunk_size: Optional[int] = 100000
    ):
        self.query = query
        self.engine = engine
        self.chunk_size = chunk_size
        self.async_session = sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )

    def _read_sql_query(self, session):
        conn = session.connection()
        return pd.read_sql_query(text(self.query), conn, chunksize=self.chunk_size)

    async def get_batched_dataframe(self) -> Iterator[pd.DataFrame]:
        try:
            if isinstance(self.engine, str):
                raise ValueError("Engine should be an SQLAlchemy engine object")

            async with self.async_session() as session:
                return await session.run_sync(self._read_sql_query)
        except Exception as e:
            logger.error(f"Error reading batched data(pandas) from SQL: {str(e)}")

    async def get_dataframe(self) -> pd.DataFrame:
        try:
            if isinstance(self.engine, str):
                raise ValueError("Engine should be an SQLAlchemy engine object")

            async with self.async_session() as session:
                return await session.run_sync(self._read_sql_query)
        except Exception as e:
            logger.error(f"Error reading data(pandas) from SQL: {str(e)}")

    async def get_daft_dataframe(self) -> daft.DataFrame:
        """
        Method to read data from SQL using daft and return as daft dataframe
        """
        try:
            # Daft uses ConnectorX to read data from SQL by default for supported connectors
            # If a connection string is passed, it will use ConnectorX to read data
            # For unsupported connectors and if directly engine is passed, it will use SQLAlchemy
            if isinstance(self.engine, str):
                return daft.read_sql(self.query, self.engine)
            return daft.read_sql(self.query, lambda: self.engine.connect())
        except Exception as e:
            logger.error(f"Error reading data(daft) from SQL: {str(e)}")

    async def get_batched_daft_dataframe(self) -> Iterator[daft.DataFrame]:
        """
        Method to read data from SQL using pandas in batches and return as daft dataframe
        We get the data using pandas since daft does not support reading data in batches
        This pandas data will then be converted to daft dataframe
        """
        try:
            if isinstance(self.engine, str):
                raise ValueError("Engine should be an SQLAlchemy engine object")

            batched_df = await self.get_batched_dataframe()
            for df in batched_df:
                daft_df = daft.from_pandas(df)
                yield daft_df
        except Exception as e:
            logger.error(f"Error reading batched data(daft) from SQL: {str(e)}")
