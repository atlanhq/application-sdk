import glob
import logging
import os
from typing import Any, AsyncIterator, Dict, List, Optional

from application_sdk.inputs import Input
from application_sdk.inputs.objectstore import ObjectStoreInput

logger = logging.getLogger(__name__)


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
        **kwargs: Dict[str, Any],
    ):
        """Initialize the Parquet input class.

        Args:
            path (str): Path to parquet file or directory containing parquet files.
            chunk_size (Optional[int], optional): Number of rows per batch.
                Defaults to 100000.
            input_prefix (Optional[str], optional): Prefix for files when reading from object store.
                If provided, files will be read from object store. Defaults to None.
            kwargs (Dict[str, Any]): Additional keyword arguments.
        """
        self.path = path
        self.chunk_size = chunk_size
        self.input_prefix = input_prefix
        self.file_names = file_names

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
        kwargs["input_prefix"] = kwargs.get("output_prefix", "")
        return cls(**kwargs)

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
                logger.info(
                    f"Reading file from object store: {remote_file_path} from {self.input_prefix}"
                )
                ObjectStoreInput.download_files_from_object_store(
                    self.input_prefix, remote_file_path
                )

    async def get_dataframe(self) -> "pd.DataFrame":
        """
        Method to read the data from the parquet file(s)
        and return as a single combined pandas dataframe.

        Returns:
            "pd.DataFrame": Combined dataframe from all parquet files.
        """
        try:
            import pandas as pd

            path = self.path
            if self.input_prefix:
                path = await self.download_files(self.path)
            # Use pandas native read_parquet which can handle both single files and directories
            return pd.read_parquet(path)
        except Exception as e:
            logger.error(f"Error reading data from parquet file(s): {str(e)}")
            # Re-raise to match IcebergInput behavior
            raise

    async def get_batched_dataframe(self) -> AsyncIterator["pd.DataFrame"]:
        """
        Method to read the data from the parquet file(s) in batches
        and return as an async iterator of pandas dataframes.

        Returns:
            AsyncIterator["pd.DataFrame"]: Async iterator of pandas dataframes.
        """
        try:
            import pandas as pd

            path = self.path
            if self.input_prefix:
                path = await self.download_files(self.path)
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

    async def get_daft_dataframe(self) -> "daft.DataFrame":  # noqa: F821
        """
        Method to read the data from the parquet file(s)
        and return as a single combined daft dataframe.

        Returns:
            daft.DataFrame: Combined daft dataframe from all parquet files.
        """
        try:
            import daft

            path = f"{self.path}{self.file_names[0].split('/')[0]}"
            await self.download_files(path)
            return daft.read_parquet(f"{path}/*.parquet")
        except Exception as e:
            logger.error(
                f"Error reading data from parquet file(s) using daft: {str(e)}"
            )
            # Re-raise to match IcebergInput behavior
            raise

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
                path = await self.download_files(self.path)
            # Use daft's native chunking through _chunk_size parameter
            df_iterator = daft.read_parquet(path, _chunk_size=self.chunk_size)
            for batch_df in df_iterator:
                yield batch_df
        except Exception as error:
            logger.error(
                f"Error reading data from parquet file(s) in batches using daft: {error}"
            )
            raise
