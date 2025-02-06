import os
from typing import Any, Dict, Iterator, List, Optional

import pandas as pd

from application_sdk.common.logger_adaptors import get_logger
from application_sdk.inputs import Input

logger = get_logger()


class JsonInput(Input):
    path: str
    chunk_size: Optional[int]
    file_suffixes: Optional[List[str]] = None

    def __init__(
        self,
        path: str,
        file_suffixes: Optional[List[str]] = None,
        chunk_size: Optional[int] = 100000,
        **kwargs: Dict[str, Any],
    ):
        """Initialize the JsonInput class.

        Args:
            path (str): The path to the input directory.
            file_suffixes (Optional[List[str]]): The file suffixes to read.
            chunk_size (Optional[int]): The chunk size to read the data.
            **kwargs (Dict[str, Any]): Keyword arguments for initialization.
        """
        self.path = path
        self.chunk_size = chunk_size
        self.file_suffixes = file_suffixes

    @classmethod
    def re_init(
        cls,
        path: str,
        **kwargs: Dict[str, Any],
    ):
        """Re-initialize the input class with given keyword arguments.

        Args:
            path (str): The additional path to the input directory.
            **kwargs (Dict[str, Any]): Keyword arguments for re-initialization.
        """
        output_path = kwargs.get("output_path", "")
        kwargs["path"] = f"{output_path}{path}"
        return cls(**kwargs)

    def get_batched_dataframe(self) -> Iterator[pd.DataFrame]:
        """
        Method to read the data from the json files in the path
        and return as a batched pandas dataframe
        """
        try:
            for file_suffix in self.file_suffixes or []:
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
            for file_suffix in self.file_suffixes or []:
                dataframes.append(
                    pd.read_json(
                        os.path.join(self.path, file_suffix),
                        lines=True,
                    )
                )
            return pd.concat(dataframes, ignore_index=True)
        except Exception as e:
            logger.error(f"Error reading data from JSON: {str(e)}")

    def get_batched_daft_dataframe(self) -> Iterator["daft.DataFrame"]:  # noqa: F821
        """
        Method to read the data from the json files in the path
        and return as a batched daft dataframe
        """
        try:
            import daft

            for file_suffix in self.file_suffixes or []:
                json_reader_obj = daft.read_json(
                    path=os.path.join(self.path, file_suffix),
                    _chunk_size=self.chunk_size,
                )
                yield json_reader_obj
        except Exception as e:
            logger.error(f"Error reading batched data from JSON: {str(e)}")

    def get_daft_dataframe(self) -> "daft.DataFrame":  # noqa: F821
        """
        Method to read the data from the json files in the path
        and return as a single combined daft dataframe
        """
        try:
            import daft

            dataframes = []
            for file_suffix in self.file_suffixes or []:
                daft.read_json(path=os.path.join(self.path, file_suffix))
            return pd.concat(dataframes, ignore_index=True)
        except Exception as e:
            logger.error(f"Error reading data from JSON using daft: {str(e)}")
