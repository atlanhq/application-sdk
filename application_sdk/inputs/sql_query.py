from typing import Any, Iterator, Optional

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
    engine: Engine
    chunk_size: Optional[int]

    def __init__(self, engine: Engine, query: str, chunk_size: Optional[int] = 100000):
        self.query = query
        self.engine = engine
        self.chunk_size = chunk_size

    def get_batched_dataframe(self) -> Iterator[pd.DataFrame]:
        try:
            with self.engine.connect() as conn:
                result = conn.execute(text(self.query))
                while True:
                    chunk = result.fetchmany(self.chunk_size)
                    if not chunk:
                        break

                    for j in range(len(chunk)):
                        row = chunk[j]
                        temp_row = []
                        for i in range(len(row)):
                            if str(type(row[i])) == "<class 'datetime.datetime'>":
                                temp_row.append(
                                    row[i].strftime("%Y-%m-%dT%H:%M:%S.%f%z")
                                )
                            else:
                                temp_row.append(row[i])
                        chunk[j] = temp_row

                    yield pd.DataFrame(chunk, columns=result.keys())
        except Exception as e:
            logger.error(f"Error reading batched data from SQL: {str(e)}")

    def get_dataframe(self) -> pd.DataFrame:
        try:
            with self.engine.connect() as conn:
                result = conn.execute(text(self.query))
                rows = result.fetchall()
                processed_rows = []
                for row in rows:
                    temp_row = []
                    for val in row:
                        if str(type(val)) == "<class 'datetime.datetime'>":
                            temp_row.append(val.strftime("%Y-%m-%dT%H:%M:%S.%f%z"))
                        else:
                            temp_row.append(val)
                    processed_rows.append(temp_row)

                return pd.DataFrame(processed_rows, columns=result.keys())
        except Exception as e:
            logger.error(f"Error reading data from SQL: {str(e)}")

    def get_key(self, key: str) -> Any:
        raise AttributeError("SQLQueryInput does not support get_key method")


class AsyncSQLQueryInput(Input):
    query: str
    engine: Engine
    chunk_size: Optional[int]

    def __init__(self, engine: Engine, query: str, chunk_size: Optional[int] = 100000):
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
            async with self.async_session() as session:
                return await session.run_sync(self._read_sql_query)
        except Exception as e:
            logger.error(f"Error reading batched data from SQL: {str(e)}")

    async def get_dataframe(self) -> pd.DataFrame:
        try:
            async with self.async_session() as session:
                return await session.run_sync(self._read_sql_query)
        except Exception as e:
            logger.error(f"Error reading data from SQL: {str(e)}")

    def get_key(self, key: str) -> Any:
        raise AttributeError("SQLQueryInput does not support get_key method")
