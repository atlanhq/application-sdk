import logging
from typing import Any, AsyncIterator, Dict, Optional

import daft
import pandas as pd

from application_sdk.inputs import Input

logger = logging.getLogger(__name__)


class ParquetInput(Input):
    """
    Parquet Input class to read data from Parquet files using daft and pandas.
    Supports reading both single files and directories containing multiple parquet files.
    """

    def __init__(
        self,
        file_path: str,
        chunk_size: Optional[int] = 100000,
        **kwargs: Dict[str, Any],
    ):
        """Initialize the Parquet input class.

        Args:
            file_path (str): Path to parquet file or directory containing parquet files.
            chunk_size (Optional[int], optional): Number of rows per batch.
                Defaults to 100000.
            kwargs (Dict[str, Any]): Additional keyword arguments.
        """
        self.file_path = file_path
        self.chunk_size = chunk_size

    def get_dataframe(self) -> pd.DataFrame:
        """
        Method to read the data from the parquet file(s)
        and return as a single combined pandas dataframe.

        Returns:
            pd.DataFrame: Combined dataframe from all parquet files.
        """
        try:
            # Use pandas native read_parquet which can handle both single files and directories
            return pd.read_parquet(self.file_path)
        except Exception as e:
            logger.error(f"Error reading data from parquet file(s): {str(e)}")
            # Re-raise to match IcebergInput behavior
            raise

    async def get_batched_dataframe(self) -> AsyncIterator[pd.DataFrame]:
        """
        Method to read the data from the parquet file(s) in batches
        and return as an async iterator of pandas dataframes.

        Returns:
            AsyncIterator[pd.DataFrame]: Async iterator of pandas dataframes.
        """
        try:
            df = pd.read_parquet(self.file_path)
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

    def get_daft_dataframe(self) -> "daft.DataFrame":  # noqa: F821
        """
        Method to read the data from the parquet file(s)
        and return as a single combined daft dataframe.

        Returns:
            daft.DataFrame: Combined daft dataframe from all parquet files.
        """
        try:
            return daft.read_parquet(self.file_path)
        except Exception as e:
            logger.error(
                f"Error reading data from parquet file(s) using daft: {str(e)}"
            )
            # Re-raise to match IcebergInput behavior
            raise

    async def get_batched_daft_dataframe(self) -> AsyncIterator[daft.DataFrame]:
        """
        Get batched daft dataframe from parquet file(s)
        """
        try:
            df = daft.read_parquet(self.file_path)
            total_rows = df.count_rows()

            # Materialize the DataFrame
            df = df.collect()

            for i in range(0, total_rows, self.chunk_size):
                # Create a new DataFrame for each batch
                batch_df = daft.from_pandas(
                    df[i : min(i + self.chunk_size, total_rows)]
                )
                yield batch_df
        except Exception as error:
            logger.error(
                f"Error reading data from parquet file(s) in batches using daft: {error}"
            )
            raise
