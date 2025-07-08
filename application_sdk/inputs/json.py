import os
from typing import TYPE_CHECKING, AsyncIterator, Iterator, List, Optional, Union

from application_sdk.common.error_codes import IOError
from application_sdk.inputs import Input
from application_sdk.inputs.objectstore import ObjectStoreInput
from application_sdk.observability.logger_adaptor import get_logger

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
        # If chunk_size is provided, use it; otherwise default to 100,000 rows per batch
        self.chunk_size = chunk_size if chunk_size is not None else 100000
        self.file_names = file_names
        self.download_file_prefix = download_file_prefix

    async def download_files(self):
        """Download the files from the object store to the local path"""
        if not self.file_names:
            logger.debug("No files to download")
            return

        for file_name in self.file_names or []:
            try:
                if self.download_file_prefix is not None and not os.path.exists(
                    os.path.join(self.path, file_name)
                ):
                    ObjectStoreInput.download_file_from_object_store(
                        os.path.join(self.download_file_prefix, file_name),
                        os.path.join(self.path, file_name),
                    )
            except IOError as e:
                logger.error(
                    f"{IOError.OBJECT_STORE_DOWNLOAD_ERROR}: Error downloading file {file_name}: {str(e)}",
                    error_code=IOError.OBJECT_STORE_DOWNLOAD_ERROR.code,
                )
                raise IOError(
                    f"{IOError.OBJECT_STORE_DOWNLOAD_ERROR}: Error downloading file {file_name}: {str(e)}"
                )

    async def get_batched_dataframe(
        self,
    ) -> Union[AsyncIterator["pd.DataFrame"], Iterator["pd.DataFrame"]]:
        """
        Method to read the data from the json files in the path
        and return as a batched pandas dataframe
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
        except IOError as e:
            logger.error(
                f"{IOError.OBJECT_STORE_DOWNLOAD_ERROR}: Error reading batched data from JSON: {str(e)}",
                error_code=IOError.OBJECT_STORE_DOWNLOAD_ERROR.code,
            )
            raise

    async def get_dataframe(self) -> "pd.DataFrame":
        """
        Method to read the data from the json files in the path
        and return as a single combined pandas dataframe
        """
        try:
            import pandas as pd

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
        except IOError as e:
            logger.error(
                f"{IOError.OBJECT_STORE_DOWNLOAD_ERROR}: Error reading data from JSON: {str(e)}",
                error_code=IOError.OBJECT_STORE_DOWNLOAD_ERROR.code,
            )
            raise

    async def get_batched_daft_dataframe(
        self,
    ) -> Union[AsyncIterator["daft.DataFrame"], Iterator["daft.DataFrame"]]:  # noqa: F821
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
        except IOError as e:
            logger.error(
                f"{IOError.OBJECT_STORE_DOWNLOAD_ERROR}: Error reading batched data from JSON: {str(e)}",
                error_code=IOError.OBJECT_STORE_DOWNLOAD_ERROR.code,
            )
            raise

    async def get_daft_dataframe(self) -> "daft.DataFrame":  # noqa: F821
        """
        Method to read the data from the json files in the path
        and return as a single combined daft dataframe
        """
        try:
            import daft

            await self.download_files()
            if not self.file_names or len(self.file_names) == 0:
                raise ValueError("No files to read")
            directory = os.path.join(self.path, self.file_names[0].split("/")[0])
            return daft.read_json(path=f"{directory}/*.json")
        except IOError as e:
            logger.error(
                f"{IOError.OBJECT_STORE_DOWNLOAD_ERROR}: Error reading data from JSON using daft: {str(e)}",
                error_code=IOError.OBJECT_STORE_DOWNLOAD_ERROR.code,
            )
            raise
