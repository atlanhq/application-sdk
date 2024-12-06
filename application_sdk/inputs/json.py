import os
from typing import Any, Iterator, List, Optional

import pandas as pd

from application_sdk import logging
from application_sdk.inputs import Input

logger = logging.get_logger(__name__)


class JsonInput(Input):
    path: str
    chunk_size: Optional[int]
    batch: List[str]

    def __init__(self, path: str, batch: List[str], chunk_size: Optional[int] = 100000):
        self.path = path
        self.chunk_size = chunk_size
        self.batch = batch

    def get_batched_dataframe(self) -> Iterator[pd.DataFrame]:
        try:
            for chunk in self.batch:
                json_reader_obj = pd.read_json(
                    os.path.join(self.path, chunk),
                    chunksize=self.chunk_size,
                    lines=True,
                )
                yield from json_reader_obj
        except Exception as e:
            logger.error(f"Error reading batched data from JSON: {str(e)}")

    def get_dataframe(self) -> pd.DataFrame:
        try:
            dataframes = []
            for chunk in self.batch:
                dataframes.append(
                    pd.read_json(
                        os.path.join(self.path, chunk),
                        lines=True,
                    )
                )
            return pd.concat(dataframes, ignore_index=True)
        except Exception as e:
            logger.error(f"Error reading data from JSON: {str(e)}")

    def get_key(self, key: str) -> Any:
        raise AttributeError("JSON does not support get_key method")
