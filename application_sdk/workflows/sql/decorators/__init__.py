import abc
import os
from abc import abstractmethod
from typing import Optional, List

from sqlalchemy.engine import Engine
import pandas as pd


class Input(abc.ABC):
    @abstractmethod
    def get_batched_df(self):
        pass

class QueryInput(Input):
    def __init__(self, engine: Engine, query: str):
        self.query = query
        self.engine = engine

    def get_batched_df(self):
        with self.engine.connect() as conn:
            data_iter = pd.read_sql_query(
                self.query, conn, chunksize=10000
            )
            for chunk_df in data_iter:
                yield chunk_df


class Output(abc.ABC):
    @abstractmethod
    def write_df(self, df: pd.DataFrame):
        pass

class JsonOutput(Output):
    def __init__(self, output_path: str):
        self.output_path = output_path
        os.makedirs(f"{output_path}", exist_ok=True)

    def write_df(self, df: pd.DataFrame, chunk_num=0):
        df.to_json(f"{self.output_path}/{chunk_num}.json", orient="records", lines=True)

def transform(
        source_df: Input,
        outputs: Optional[List[Output]] = None
):
    def decorator(f):
        async def new_fn(*args, **kwargs):
            for chunk_df in source_df.get_batched_df():
                dfs: Optional[List[pd.DataFrame]] = await f(chunk_df, *args, **kwargs)
                if not dfs or not outputs:
                    continue

                for i, df in enumerate(dfs):
                    outputs[i].write_df(df)
        return new_fn
    return decorator


