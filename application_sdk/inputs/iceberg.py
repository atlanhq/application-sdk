import os
from typing import Iterator, List, Optional

import pandas as pd
from pyiceberg.table import Table

from application_sdk.common.error_codes import ApplicationFrameworkErrorCodes
from application_sdk.common.logger_adaptors import get_logger
from application_sdk.inputs import Input
from application_sdk.inputs.objectstore import ObjectStoreInput

logger = get_logger(__name__)


class IcebergInput(Input):
    """
    Iceberg Input class to read data from Iceberg tables using daft and pandas
    """

    table: Table
    chunk_size: Optional[int]
    file_names: List[str]
    path: str
    download_file_prefix: str

    def __init__(
        self,
        table: Table,
        chunk_size: Optional[int] = 100000,
        file_names: List[str] = [],
        path: str = "",
        download_file_prefix: str = "",
    ):
        """Initialize the Iceberg input class.

        Args:
            table (Table): Iceberg table object.
            chunk_size (Optional[int], optional): Number of rows per batch.
                Defaults to 100000.
            file_names (List[str], optional): List of file names to download.
                Defaults to [].
            path (str, optional): Path to download files.
                Defaults to "".
            download_file_prefix (str, optional): Prefix for downloading files.
                Defaults to "".
        """
        self.table = table
        self.chunk_size = chunk_size
        self.file_names = file_names
        self.path = path
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
                logger.error(
                    f"Error downloading file {file_name}: {str(e)}",
                    error_code=ApplicationFrameworkErrorCodes.InputErrorCodes.ICEBERG_DOWNLOAD_ERROR,
                )
                raise e

    async def get_dataframe(self) -> "pd.DataFrame":
        """
        Method to read the data from the iceberg files in the path
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
                f"Error reading data from Iceberg: {str(e)}",
                error_code=ApplicationFrameworkErrorCodes.InputErrorCodes.ICEBERG_READ_ERROR,
            )

    def get_batched_dataframe(self) -> Iterator["pd.DataFrame"]:
        # We are not implementing this method as we have to parition the daft dataframe
        # using dataframe.into_partitions() method. This method does all the paritions in memory
        # and using that can cause out of memory issues.
        # ref: https://www.getdaft.io/projects/docs/en/stable/user_guide/poweruser/partitioning.html
        raise NotImplementedError

    def get_daft_dataframe(self) -> "daft.DataFrame":  # noqa: F821
        """
        Method to read the data from the iceberg table
        and return as a single combined daft dataframe
        """
        try:
            import daft

            return daft.read_iceberg(self.table)
        except Exception as e:
            logger.error(f"Error reading data from Iceberg table using daft: {str(e)}")

    def get_batched_daft_dataframe(self) -> Iterator["daft.DataFrame"]:  # noqa: F821
        # We are not implementing this method as we have to parition the daft dataframe
        # using dataframe.into_partitions() method. This method does all the paritions in memory
        # and using that can cause out of memory issues.
        # ref: https://www.getdaft.io/projects/docs/en/stable/user_guide/poweruser/partitioning.html
        raise NotImplementedError
