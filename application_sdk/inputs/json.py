import os
from typing import Any, Dict, Iterator, List, Optional

import pandas as pd

from application_sdk.common.logger_adaptors import get_logger
from application_sdk.config import get_settings
from application_sdk.inputs import Input
from application_sdk.inputs.objectstore import ObjectStoreInput

logger = get_logger(__name__)


class JsonInput(Input):
    path: str
    chunk_size: Optional[int]
    file_names: Optional[List[str]]
    download_file_prefix: Optional[str]

    def __init__(
        self,
        path: str,
        file_names: Optional[List[str]] = None,
        download_file_prefix: Optional[str] = None,
        chunk_size: Optional[int] = None,
        **kwargs: Dict[str, Any],
    ):
        """Initialize the JsonInput class.

        Args:
            path (str): The path to the input directory.
            file_names (Optional[List[str]]): The list of files to read.
            download_file_prefix (Optional[str]): The prefix path in object store.
            chunk_size (Optional[int]): The chunk size to read the data. If None, uses config value.
            **kwargs (Dict[str, Any]): Keyword arguments for initialization.
        """
        self.path = path
        settings = get_settings()
        self.chunk_size = chunk_size or settings.chunk_size
        self.file_names = file_names
        self.download_file_prefix = download_file_prefix

    async def download_files(self):
        """Download the files from the object store to the local path"""
        if not self.file_names:
            logger.debug("No files to download")
            return

        for file_name in self.file_names or []:
            try:
                if not os.path.exists(os.path.join(self.path, file_name)):
                    ObjectStoreInput.download_file_from_object_store(
                        os.path.join(self.download_file_prefix, file_name),
                        os.path.join(self.path, file_name),
                    )
            except Exception as e:
                logger.error(f"Error downloading file {file_name}: {str(e)}")
                raise e

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
        kwargs["download_file_prefix"] = kwargs.get("output_prefix", "")
        return cls(**kwargs)

    async def get_batched_dataframe(self) -> Iterator[pd.DataFrame]:
        """
        Method to read the data from the json files in the path
        and return as a batched pandas dataframe
        """
        try:
            await self.download_files()

            for file_name in self.file_names or []:
                json_reader_obj = pd.read_json(
                    os.path.join(self.path, file_name),
                    chunksize=self.chunk_size,
                    lines=True,
                )
                for chunk in json_reader_obj:
                    yield chunk
        except Exception as e:
            logger.error(f"Error reading batched data from JSON: {str(e)}")

    async def get_dataframe(self) -> pd.DataFrame:
        """
        Method to read the data from the json files in the path
        and return as a single combined pandas dataframe
        """
        try:
            dataframes = []
            await self.download_files()
            for file_name in self.file_names or []:
                dataframes.append(
                    pd.read_json(
                        os.path.join(self.path, file_name),
                        lines=True,
                    )
                )
            return pd.concat(dataframes, ignore_index=True)
        except Exception as e:
            logger.error(f"Error reading data from JSON: {str(e)}")

    async def get_batched_daft_dataframe(self) -> Iterator["daft.DataFrame"]:  # noqa: F821
        """
        Method to read the data from the json files in the path
        and return as a batched daft dataframe
        """
        try:
            import daft

            await self.download_files()
            for file_name in self.file_names or []:
                json_reader_obj = daft.read_json(
                    path=os.path.join(self.path, file_name),
                    _chunk_size=self.chunk_size,
                )
                yield json_reader_obj
        except Exception as e:
            logger.error(f"Error reading batched data from JSON: {str(e)}")

    async def get_daft_dataframe(self) -> "daft.DataFrame":  # noqa: F821
        """
        Method to read the data from the json files in the path
        and return as a single combined daft dataframe
        """
        try:
            import daft

            dataframe_concat = None
            await self.download_files()
            for file_name in self.file_names or []:
                json_dataframe = daft.read_json(path=os.path.join(self.path, file_name))
                dataframe_concat = (
                    json_dataframe
                    if dataframe_concat is None
                    else dataframe_concat.concat(json_dataframe)
                )
            return dataframe_concat
        except Exception as e:
            logger.error(f"Error reading data from JSON using daft: {str(e)}")
