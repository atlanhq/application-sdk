from typing import Any, Iterator

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

from application_sdk import logging
from application_sdk.inputs import Input

logger = logging.get_logger(__name__)


class SQLQueryInput(Input):
    query: str | None
    engine: Engine | None
    chunk_size: int | None

    def __init__(
        self, engine: Engine, query: str | None = None, chunk_size: int = 100000
    ):
        self.query = query
        self.engine = engine
        self.chunk_size = chunk_size

    def get_batched_df(self) -> Iterator[pd.DataFrame]:
        try:
            with self.engine.connect() as conn:
                return pd.read_sql_query(
                    text(self.query), conn, chunksize=self.chunk_size
                )
        except Exception as e:
            logger.error(f"Error reading batched data from SQL: {str(e)}")

    def get_df(self) -> pd.DataFrame:
        try:
            with self.engine.connect() as conn:
                return pd.read_sql_query(text(self.query), conn)
        except Exception as e:
            logger.error(f"Error reading data from SQL: {str(e)}")

    def get_key(self, key: str) -> Any:
        raise AttributeError("SQLQueryInput does not support get_key method")
