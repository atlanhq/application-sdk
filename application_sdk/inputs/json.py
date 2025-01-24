import logging
import os
from typing import Any, Dict, Iterator, List, Optional

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
        path: Optional[str] = None,
        file_suffixes: Optional[List[str]] = None,
        chunk_size: Optional[int] = 100000,
        **kwargs: Dict[str, Any],
    ):
        self.path = path
        self.chunk_size = chunk_size
        self.file_suffixes = file_suffixes

    @classmethod
    def re_init(
        cls,
        output_path: str,
        path: str,
        batch: Optional[List[str]],
        **kwargs: Dict[str, Any],
    ):
        """Re-initialize the input class with given keyword arguments.

        Args:
            output_path (str): The base path to the output directory.
            path (str): The additional path to the output directory.
            batch (Optional[List[str]]): The list of file suffixes.
            **kwargs (Dict[str, Any]): Keyword arguments for re-initialization.
        """
        kwargs["path"] = f"{output_path}{path}"
        kwargs["file_suffixes"] = batch
        return cls(**kwargs)

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
