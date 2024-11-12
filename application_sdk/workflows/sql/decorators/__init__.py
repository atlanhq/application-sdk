import abc
import os
from abc import abstractmethod
from collections.abc import Iterator
from typing import Optional, Dict

from sqlalchemy.engine import Engine
import pandas as pd

from application_sdk import logging

logger = logging.get_logger(__name__)


class Input(abc.ABC):
    @abstractmethod
    def get_batched_df(self) -> Iterator[pd.DataFrame]:
        pass

    @abstractmethod
    def get_df(self) -> pd.DataFrame:
        pass

class QueryInput(Input):
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


class ObjectStoreInput(Input):
    def __init__(self, object_store_path: str):
        self.object_store_path = object_store_path

    def get_batched_df(self) -> Iterator[pd.DataFrame]:
        for file in os.listdir(self.object_store_path):
            if file.endswith(".json"):
                yield pd.read_json(f"{self.object_store_path}/{file}", lines=True)

    def get_df(self) -> pd.DataFrame:
        return pd.concat([df for df in self.get_batched_df()])


class Output(abc.ABC):
    @abstractmethod
    def write_df(self, df: pd.DataFrame, chunk_num: int=0):
        pass

class JsonOutput(Output):
    def __init__(self, output_path: str):
        self.output_path = output_path
        os.makedirs(f"{output_path}", exist_ok=True)

    def write_df(self, df: pd.DataFrame, chunk_num: int=0):
        df.to_json(f"{self.output_path}/{chunk_num}.json", orient="records", lines=True)

def transform(batch_input: Optional[Input]=None, **kwargs):
    def decorator(f):
        async def new_fn():
            fn_kwargs = {}
            outputs = {}
            for name, arg in kwargs.items():
                if isinstance(arg, Input):
                    fn_kwargs[name] = arg.get_df()
                elif isinstance(arg, Output):
                    fn_kwargs[name] = arg
                    outputs[name] = arg
                else:
                    fn_kwargs[name] = arg

            def process_ret(ret: Optional[Dict[str, pd.DataFrame]]):
                if not ret or type(ret) != dict:
                    logger.info("Function did not return any data")
                    return

                for ret_name, ret_df in ret.items():
                    if ret_name not in outputs:
                        logger.warning(f"Output {ret_name} not found but function returned data")
                        continue
                    outputs[ret_name].write_df(ret_df)

            if not batch_input:
                process_ret(await f(**fn_kwargs))
                return

            for df in batch_input.get_batched_df():
                fn_kwargs['batch_input'] = df
                process_ret(await f(**fn_kwargs))
                del fn_kwargs['batch_input']

        return new_fn
    return decorator