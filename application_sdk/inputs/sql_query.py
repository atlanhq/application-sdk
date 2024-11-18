from typing import Iterator, Any
from application_sdk.inputs import Input
from sqlalchemy.engine import Engine
import pandas as pd

class SQLQueryInput(Input):
    def __init__(self, engine: Engine, query: str):
        self.query = query
        self.engine = engine

    def get_batched_df(self) -> Iterator[pd.DataFrame]:
        with self.engine.connect() as conn:
            return pd.read_sql_query(
                self.query, conn, chunksize=10000
            )

    def get_df(self) -> pd.DataFrame:
        with self.engine.connect() as conn:
            return pd.read_sql_query(self.query, conn)

    def get_key(self, key: str) -> Any:
        raise AttributeError("SQLQueryInput does not support get_key method")