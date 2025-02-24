from typing import Any, Dict, Optional

import pandas as pd

from application_sdk.common.logger_adaptors import get_logger
from application_sdk.inputs import Input

logger = get_logger(__name__)


class ParquetInput(Input):
    """
    Parquet Input class to read data from Parquet files using daft and pandas.
    Supports reading both single files and directories containing multiple parquet files.
    """

    def __init__(
        self, file_path: str, chunk_size: Optional[int] = 100000, **kwargs: Dict[str, Any]
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

    def get_daft_dataframe(self) -> "daft.DataFrame":  # noqa: F821
        """
        Method to read the data from the parquet file(s)
        and return as a single combined daft dataframe.

        Returns:
            daft.DataFrame: Combined daft dataframe from all parquet files.
        """
        try:
            import daft
            return daft.read_parquet(self.file_path)
        except Exception as e:
            logger.error(f"Error reading data from parquet file(s) using daft: {str(e)}")
            # Re-raise to match IcebergInput behavior
            raise 