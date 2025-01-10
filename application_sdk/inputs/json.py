import asyncio
import logging
import os
from typing import Iterator, List, Optional

import daft
import pandas as pd

from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.inputs import Input
from application_sdk.inputs.objectstore import ObjectStore

logger = AtlanLoggerAdapter(logging.getLogger(__name__))


class JsonInput(Input):
    path: str
    chunk_size: Optional[int]
    file_names: List[str]

    def __init__(
        self,
        path: str,
        file_names: List[str],
        download_file_prefix: str,
        chunk_size: Optional[int] = 100000,
    ):
        self.path = path
        self.chunk_size = chunk_size
        self.file_names = file_names
        self.download_file_prefix = download_file_prefix

    async def download_files(self):
        for file_name in self.file_names:
            if not os.path.exists(os.path.join(self.path, file_name)):
                await ObjectStore.download_file_from_object_store(
                    os.path.join(self.download_file_prefix, file_name),
                    os.path.join(self.path, file_name),
                )

    def get_batched_dataframe(self) -> Iterator[pd.DataFrame]:
        """
        Method to read the data from the json files in the path
        and return as a batched pandas dataframe
        """
        try:
            asyncio.run(self.download_files())

            for file_name in self.file_names:
                json_reader_obj = pd.read_json(
                    os.path.join(self.path, file_name),
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
            asyncio.run(self.download_files())
            for file_name in self.file_names:
                dataframes.append(
                    pd.read_json(
                        os.path.join(self.path, file_name),
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
            asyncio.run(self.download_files())
            for file_name in self.file_names:
                json_reader_obj = daft.read_json(
                    path=os.path.join(self.path, file_name),
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
            asyncio.run(self.download_files())
            for file_name in self.file_names:
                dataframes.append(
                    daft.read_json(path=os.path.join(self.path, file_name))
                )
            return pd.concat(dataframes, ignore_index=True)
        except Exception as e:
            logger.error(f"Error reading data from JSON using daft: {str(e)}")
