import logging
import os
from typing import Iterator, List, Optional

import daft
import pandas as pd

from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.inputs import Input

logger = AtlanLoggerAdapter(logging.getLogger(__name__))


class JsonInput(Input):
    path: Optional[str]
    path_suffix: Optional[str]
    chunk_size: Optional[int]
    file_suffixes: Optional[List[str]] = None
    test: int = 0

    def __init__(
        self,
        path_suffix: Optional[str],
        path: Optional[str] = None,
        file_suffixes: Optional[List[str]] = None,
        chunk_size: Optional[int] = 100000,
    ):
        self.path = path
        self.chunk_size = chunk_size
        self.file_suffixes = file_suffixes
        self.path_suffix = path_suffix

    def re_init(self, output_path, batch, **kwargs):
        self.path = f"{output_path}{self.path_suffix}"
        self.file_suffixes = batch

    def get_batched_dataframe(self) -> Iterator[pd.DataFrame]:
        """
        Method to read the data from the json files in the path
        and return as a batched pandas dataframe
        """
        try:
            for file_suffix in self.file_suffixes:
                json_reader_obj = pd.read_json(
                    os.path.join(self.path, file_suffix),
                    chunksize=self.chunk_size,
                    lines=True,
                )
                yield from json_reader_obj
        except Exception as e:
            logger.error(f"Error reading batched data from JSON: {str(e)}")

    def get_dataframe(self) -> pd.DataFrame:
        """
        Method to read the data from the json files in the path
        and return as a single combined pandas dataframe
        """
        try:
            dataframes = []
            for file_suffix in self.file_suffixes:
                dataframes.append(
                    pd.read_json(
                        os.path.join(self.path, file_suffix),
                        lines=True,
                    )
                )
            return pd.concat(dataframes, ignore_index=True)
        except Exception as e:
            logger.error(f"Error reading data from JSON: {str(e)}")

    def get_batched_daft_dataframe(self) -> Iterator[daft.DataFrame]:
        """
        Method to read the data from the json files in the path
        and return as a batched daft dataframe
        """
        try:
            for file_suffix in self.file_suffixes:
                json_reader_obj = daft.read_json(
                    path=os.path.join(self.path, file_suffix),
                    _chunk_size=self.chunk_size,
                )
                yield json_reader_obj
        except Exception as e:
            logger.error(f"Error reading batched data from JSON: {str(e)}")

    def get_daft_dataframe(self) -> daft.DataFrame:
        """
        Method to read the data from the json files in the path
        and return as a single combined daft dataframe
        """
        try:
            dataframes = []
            for file_suffix in self.file_suffixes:
                daft.read_json(path=os.path.join(self.path, file_suffix))
            return pd.concat(dataframes, ignore_index=True)
        except Exception as e:
            logger.error(f"Error reading data from JSON using daft: {str(e)}")
