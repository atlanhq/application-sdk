import glob
import logging
import os
from typing import TYPE_CHECKING, AsyncIterator, Iterator, List, Optional, Union

from application_sdk.inputs import Input
from application_sdk.inputs.objectstore import ObjectStoreInput

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    import daft
    import pandas as pd


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

    async def download_files(self, remote_file_path: str) -> str:
        """Read a file from the object store.

        Args:
            remote_file_path (str): Path to the remote file in object store.

        Returns:
            str: Path to the downloaded local file.
        """
        if os.path.isdir(remote_file_path):
            parquet_files = glob.glob(os.path.join(remote_file_path, "*.parquet"))
            if not parquet_files:
                if self.input_prefix is not None:
                    logger.info(
                        f"Reading file from object store: {remote_file_path} from {self.input_prefix}"
                    )
                    ObjectStoreInput.download_files_from_object_store(
                        self.input_prefix, remote_file_path
                    )
        # Return the path to maintain the contract
        return remote_file_path

    def get_dataframe(self) -> "pd.DataFrame":
        """
        Method to read the data from the parquet file(s)
        and return as a single combined pandas dataframe.

        Returns:
            "pd.DataFrame": Combined dataframe from all parquet files.
        """
        import asyncio

        return asyncio.run(self._get_dataframe_async())

    async def _get_dataframe_async(self) -> "pd.DataFrame":
        """Async implementation of get_dataframe"""
        try:
            import pandas as pd

            path = self.path
            if self.input_prefix is not None and self.path is not None:
                path = await self.download_files(self.path)
            # Use pandas native read_parquet which can handle both single files and directories
            if path is None:
                return pd.DataFrame()
            return pd.read_parquet(path)
        except Exception as e:
            logger.error(f"Error reading data from parquet file(s): {str(e)}")
            # Re-raise to match IcebergInput behavior
            raise

    def get_batched_dataframe(self) -> Iterator["pd.DataFrame"]:
        """
        Method to read the data from the parquet file(s) in batches
        and return as an iterator of pandas dataframes.

        Returns:
            Iterator["pd.DataFrame"]: Iterator of pandas dataframes.
        """
        import asyncio

        async def _wrapper_coroutine():
            result = []
            async for item in self._get_batched_dataframe_async():
                result.append(item)
            return result

        return iter(asyncio.run(_wrapper_coroutine()))

    async def _get_batched_dataframe_async(self) -> AsyncIterator["pd.DataFrame"]:
        """Async implementation of get_batched_dataframe"""
        try:
            import pandas as pd

            path = self.path
            if self.input_prefix is not None and self.path is not None:
                path = await self.download_files(self.path)

            if path is None:
                return

            df = pd.read_parquet(path)
            if self.chunk_size:
                for i in range(0, len(df), self.chunk_size):
                    yield df.iloc[i : i + self.chunk_size]
            else:
                yield df
        except Exception as e:
            logger.error(
                f"Error reading data from parquet file(s) in batches: {str(e)}"
            )
            raise

    def get_daft_dataframe(self) -> "daft.DataFrame":
        """
        Method to read the data from the parquet file(s)
        and return as a single combined daft dataframe.

        Returns:
            daft.DataFrame: Combined daft dataframe from all parquet files.
        """
        import asyncio

        return asyncio.run(self._get_daft_dataframe_async())

    async def _get_daft_dataframe_async(self) -> "daft.DataFrame":
        """Async implementation of get_daft_dataframe"""
        try:
            import daft

            if self.file_names is not None and len(self.file_names) > 0:
                first_file = self.file_names[0]
                parts = first_file.split("/")
                if parts and self.path is not None:
                    path = f"{self.path}/{parts[0]}"
                else:
                    path = self.path
            else:
                path = self.path

            if path is not None:
                await self.download_files(path)
                return daft.read_parquet(f"{path}/*.parquet")
            else:
                return daft.DataFrame()
        except Exception as e:
            logger.error(
                f"Error reading data from parquet file(s) using daft: {str(e)}"
            )
            # Re-raise to match IcebergInput behavior
            raise

    def get_batched_daft_dataframe(
        self,
    ) -> Union[Iterator["daft.DataFrame"], AsyncIterator["daft.DataFrame"]]:
        """
        Get batched daft dataframe from parquet file(s)

        Returns:
            Union[Iterator["daft.DataFrame"], AsyncIterator["daft.DataFrame"]]: An iterator of daft DataFrames
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
        """Async implementation of get_batched_daft_dataframe"""
        try:
            import daft

            path = self.path
            if self.input_prefix is not None and self.path is not None:
                path = await self.download_files(self.path)

            if path is None:
                return

            # Use daft's native chunking through _chunk_size parameter
            df_iterator = daft.read_parquet(path, _chunk_size=self.chunk_size)
            for batch_df in df_iterator:
                yield batch_df
        except Exception as error:
            logger.error(
                f"Error reading data from parquet file(s) in batches using daft: {error}"
            )
            raise
