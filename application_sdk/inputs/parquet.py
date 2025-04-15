import os
from typing import AsyncIterator, Iterator, List, Optional

import daft
import pandas as pd

from application_sdk.common.error_codes import ApplicationFrameworkErrorCodes
from application_sdk.common.logger_adaptors import get_logger
from application_sdk.inputs import Input
from application_sdk.inputs.objectstore import ObjectStoreInput

logger = get_logger(__name__)


class ParquetInput(Input):
    """
    Parquet Input class to read data from Parquet files using daft and pandas.
    Supports reading both single files and directories containing multiple parquet files.
    """

    def __init__(
        self,
        path: Optional[str] = None,
        chunk_size: Optional[int] = 100000,
        input_prefix: Optional[str] = None,
        file_names: Optional[List[str]] = None,
    ):
        """Initialize the Parquet input class.

        Args:
            path (str): Path to parquet file or directory containing parquet files.
            chunk_size (Optional[int], optional): Number of rows per batch.
                Defaults to 100000.
            input_prefix (Optional[str], optional): Prefix for files when reading from object store.
                If provided, files will be read from object store. Defaults to None.
            file_names (Optional[List[str]], optional): List of file names to read.
                Defaults to None.
        """
        self.path = path
        self.chunk_size = chunk_size
        self.input_prefix = input_prefix
        self.file_names = file_names

    async def download_files(self):
        """Download the files from the object store to the local path"""
        if not self.file_names:
            logger.debug("No files to download")
            return

        for file_name in self.file_names or []:
            try:
                if not os.path.exists(os.path.join(self.path, file_name)):
                    ObjectStoreInput.download_file_from_object_store(
                        os.path.join(self.input_prefix, file_name),
                        os.path.join(self.path, file_name),
                    )
            except Exception as e:
                logger.error(
                    f"Error downloading file {file_name}: {str(e)}",
                    error_code=ApplicationFrameworkErrorCodes.InputErrorCodes.PARQUET_DOWNLOAD_ERROR,
                )
                raise e

    async def get_dataframe(self) -> "pd.DataFrame":
        """
        Method to read the data from the parquet files in the path
        and return as a single combined pandas dataframe
        """
        try:
            import pandas as pd

            await self.download_files()
            if len(self.file_names or []) == 1:
                return pd.read_parquet(os.path.join(self.path, self.file_names[0]))
            else:
                return pd.read_parquet(os.path.join(self.path, "*.parquet"))
        except Exception as e:
            logger.error(
                f"Error reading data from Parquet: {str(e)}",
                error_code=ApplicationFrameworkErrorCodes.InputErrorCodes.PARQUET_READ_ERROR,
            )

    async def get_batched_dataframe(self) -> Iterator["pd.DataFrame"]:
        """
        Method to read the data from the parquet files in the path
        and return as a batched pandas dataframe
        """
        try:
            import pandas as pd

            await self.download_files()
            for file_name in self.file_names or []:
                yield pd.read_parquet(os.path.join(self.path, file_name))
        except Exception as e:
            logger.error(
                f"Error reading batched data from Parquet: {str(e)}",
                error_code=ApplicationFrameworkErrorCodes.InputErrorCodes.PARQUET_BATCH_ERROR,
            )

    async def get_daft_dataframe(self) -> "daft.DataFrame":  # noqa: F821
        """
        Method to read the data from the parquet files in the path
        and return as a single combined daft dataframe
        """
        try:
            import daft

            await self.download_files()
            if len(self.file_names or []) == 1:
                return daft.read_parquet(os.path.join(self.path, self.file_names[0]))
            else:
                return daft.read_parquet(os.path.join(self.path, "*.parquet"))
        except Exception as e:
            logger.error(
                f"Error reading data from Parquet using daft: {str(e)}",
                error_code=ApplicationFrameworkErrorCodes.InputErrorCodes.PARQUET_DAFT_ERROR,
            )

    async def get_batched_daft_dataframe(self) -> AsyncIterator["daft.DataFrame"]:
        """
        Get batched daft dataframe from parquet file(s)

        Returns:
            AsyncIterator[daft.DataFrame]: An async iterator of daft DataFrames, each containing
            a batch of data from the parquet file(s).
        """
        try:
            import daft

            path = self.path
            if self.input_prefix:
                path = await self.download_files()
            # Use daft's native chunking through _chunk_size parameter
            df_iterator = daft.read_parquet(path, _chunk_size=self.chunk_size)
            for batch_df in df_iterator:
                yield batch_df
        except Exception as error:
            logger.error(
                f"Error reading data from parquet file(s) in batches using daft: {error}"
            )
            raise
