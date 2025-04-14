import os
from typing import TYPE_CHECKING, AsyncIterator, Iterator, List, Optional, Union

from application_sdk.common.logger_adaptors import get_logger
from application_sdk.config import get_settings
from application_sdk.inputs import Input
from application_sdk.inputs.objectstore import ObjectStoreInput

if TYPE_CHECKING:
    import daft
    import pandas as pd

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
    ):
        """Initialize the JsonInput class.

        Args:
            path (str): The path to the input directory.
            file_names (Optional[List[str]]): The list of files to read.
            download_file_prefix (Optional[str]): The prefix path in object store.
            chunk_size (Optional[int]): The chunk size to read the data. If None, uses config value.
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

        for file_name in self.file_names:
            try:
                if self.download_file_prefix is not None and not os.path.exists(
                    os.path.join(self.path, file_name)
                ):
                    ObjectStoreInput.download_file_from_object_store(
                        os.path.join(self.download_file_prefix, file_name),
                        os.path.join(self.path, file_name),
                    )
            except Exception as e:
                logger.error(f"Error downloading file {file_name}: {str(e)}")
                raise e

    def get_batched_dataframe(self) -> Iterator["pd.DataFrame"]:
        """
        Method to read the data from the json files in the path
        and return as a batched pandas dataframe
        """
        # This is a synchronous wrapper for the async method
        import asyncio

        async def _wrapper_coroutine():
            result = []
            async for item in self._get_batched_dataframe_async():
                result.append(item)
            return result

        return iter(asyncio.run(_wrapper_coroutine()))

    async def _get_batched_dataframe_async(self) -> AsyncIterator["pd.DataFrame"]:
        """
        Async implementation of get_batched_dataframe
        """
        try:
            import pandas as pd

            await self.download_files()

            for file_name in self.file_names or []:
                file_path = os.path.join(self.path, file_name)
                json_reader_obj = pd.read_json(
                    file_path,
                    chunksize=self.chunk_size,
                    lines=True,
                )
                for chunk in json_reader_obj:
                    yield chunk
        except Exception as e:
            logger.error(f"Error reading batched data from JSON: {str(e)}")
            raise e

    def get_dataframe(self) -> "pd.DataFrame":
        """
        Method to read the data from the json files in the path
        and return as a single combined pandas dataframe
        """
        import asyncio

        return asyncio.run(self._get_dataframe_async())

    async def _get_dataframe_async(self) -> "pd.DataFrame":
        """
        Async implementation of get_dataframe
        """
        try:
            import pandas as pd

            dataframes = []
            await self.download_files()

            if not self.file_names:
                return pd.DataFrame()

            for file_name in self.file_names:
                file_path = os.path.join(self.path, file_name)
                dataframes.append(
                    pd.read_json(
                        file_path,
                        lines=True,
                    )
                )
            return pd.concat(dataframes, ignore_index=True)
        except Exception as e:
            logger.error(f"Error reading data from JSON: {str(e)}")
            raise e

    def get_batched_daft_dataframe(
        self,
    ) -> Union[Iterator["daft.DataFrame"], AsyncIterator["daft.DataFrame"]]:
        """
        Method to read the data from the json files in the path
        and return as a batched daft dataframe
        """
        import asyncio

        async def _wrapper_coroutine():
            result = []
            async for item in self._get_batched_daft_dataframe_async():
                result.append(item)
            return result

        return iter(asyncio.run(_wrapper_coroutine()))

    async def _get_batched_daft_dataframe_async(
        self,
    ) -> AsyncIterator["daft.DataFrame"]:
        """
        Async implementation of get_batched_daft_dataframe
        """
        try:
            import daft

            await self.download_files()

            if not self.file_names:
                return

            for file_name in self.file_names:
                file_path = os.path.join(self.path, file_name)
                json_reader_obj = daft.read_json(
                    path=file_path,
                    _chunk_size=self.chunk_size,
                )
                yield json_reader_obj
        except Exception as e:
            logger.error(f"Error reading batched data from JSON: {str(e)}")
            raise e

    def get_daft_dataframe(self) -> "daft.DataFrame":
        """
        Method to read the data from the json files in the path
        and return as a single combined daft dataframe
        """
        import asyncio

        return asyncio.run(self._get_daft_dataframe_async())

    async def _get_daft_dataframe_async(self) -> "daft.DataFrame":
        """
        Async implementation of get_daft_dataframe
        """
        try:
            import daft

            await self.download_files()

            if not self.file_names or len(self.file_names) == 0:
                # Return empty DataFrame if no files
                return daft.DataFrame()

            # Get directory from the first file name, safely handling potential None
            first_file = self.file_names[0]
            parts = first_file.split("/") if first_file else []

            if not parts:
                return daft.DataFrame()

            directory = os.path.join(self.path, parts[0])
            return daft.read_json(path=f"{directory}/*.json")
        except Exception as e:
            logger.error(f"Error reading data from JSON using daft: {str(e)}")
            raise e
